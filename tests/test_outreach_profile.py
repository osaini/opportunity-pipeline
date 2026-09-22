"""Company profiles for outreach: dedupe across name variants, sourced locations, and SEC Form D."""

import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx

from opportunity_app.outreach import (
    company_key,
    confirm_research,
    create_target,
    delete_target,
    get_target,
    import_targets,
    update_target,
)
from opportunity_app.outreach_contacts import find_contacts
from opportunity_app.outreach_drafting import location_line
from opportunity_app.outreach_profile import (
    enrich_targets,
    form_d_lookup,
    record_form_d,
    record_site_location,
    sec_fetcher,
    site_location,
)
from opportunity_app.schema import connect_product, ensure_product_schema

from helpers_platform import build_and_migrate, use_profile_regions
from test_outreach_discovery import company, only_for, proposals, safe_fetcher, site_transport

USER = "local-user"
TODAY = date(2026, 9, 18)


def page(url, html):
    from opportunity_app.outreach_contacts import _PageParser

    parser = _PageParser()
    parser.feed(html)
    parser.close()
    return {"url": url, "parser": parser}


def form_d_hit(name, cik, adsh, filed, location):
    return {
        "_id": f"{adsh}:primary_doc.xml",
        "_source": {
            "ciks": [cik], "display_names": [f"{name}  (CIK {cik})"], "adsh": adsh, "file_date": filed,
            "form": "D", "biz_locations": [location],
        },
    }


FORM_D_XML = """<?xml version="1.0"?>
<edgarSubmission>
  <primaryIssuer>
    <cik>0001995988</cik>
    <entityName>Bovi Robotics, Inc.</entityName>
    <issuerAddress><street1>1 Main St</street1><city>SAN CARLOS</city><stateOrCountry>CA</stateOrCountry>
      <stateOrCountryDescription>CALIFORNIA</stateOrCountryDescription><zipCode>94070</zipCode></issuerAddress>
  </primaryIssuer>
  <relatedPersonsList>
    <relatedPersonInfo>
      <relatedPersonName><firstName>Ada</firstName><lastName>Lovelace</lastName></relatedPersonName>
      <relatedPersonRelationshipList><relationship>Executive Officer</relationship></relatedPersonRelationshipList>
    </relatedPersonInfo>
  </relatedPersonsList>
  <offeringData>
    <industryGroup><industryGroupType>Other Technology</industryGroupType></industryGroup>
    <typeOfFiling><newOrAmendment><isAmendment>false</isAmendment></newOrAmendment>
      <dateOfFirstSale><value>2026-03-01</value></dateOfFirstSale></typeOfFiling>
    <offeringSalesAmounts><totalOfferingAmount>5000000</totalOfferingAmount><totalAmountSold>3200000</totalAmountSold></offeringSalesAmounts>
  </offeringData>
</edgarSubmission>"""


def sec_transport(hits, *, xml=FORM_D_XML, search_status=200):
    requested = []

    def handler(request):
        requested.append(str(request.url))
        if request.url.host == "efts.sec.gov":
            if search_status != 200:
                return httpx.Response(search_status)
            return httpx.Response(200, json={"hits": {"hits": hits}})
        if request.url.host == "www.sec.gov" and request.url.path.endswith("primary_doc.xml"):
            return httpx.Response(200, text=xml, headers={"content-type": "application/xml"})
        return httpx.Response(404)

    return httpx.MockTransport(handler), requested


BOVI_HITS = [
    # Investment vehicles that share part of the name are never the company.
    form_d_hit("Gaingels Bovi Robotics LLC", "0001986235", "0001986235-26-000001", "2026-04-01", "Burlington, VT"),
    form_d_hit("Bovi Robotics, Inc.", "0001995988", "0001995988-25-000001", "2025-01-10", "San Carlos, CA"),
    form_d_hit("Bovi Robotics, Inc.", "0001995988", "0001995988-26-000003", "2026-03-05", "San Carlos, CA"),
]


class ProfileTestCase(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        use_profile_regions(self)
        self.root = Path(self.tempdir.name)
        _, self.platform_path = build_and_migrate(self.root)
        self.conn = connect_product(self.platform_path)
        ensure_product_schema(self.conn)

    def tearDown(self):
        self.conn.close()
        self.tempdir.cleanup()

    def events(self, target_id):
        return [dict(row) for row in self.conn.execute(
            "SELECT event_type, detail FROM outreach_events WHERE target_id=? ORDER BY created_at", (target_id,),
        ).fetchall()]


class MigrationTests(unittest.TestCase):
    def test_existing_locations_keep_the_only_basis_that_can_be_known(self):
        import sqlite3

        from opportunity_app.schema import MIGRATIONS_DIR

        conn = sqlite3.connect(":memory:")
        try:
            migrations = sorted(MIGRATIONS_DIR.glob("[0-9][0-9][0-9][0-9]_*.sql"))
            for migration in migrations:
                if migration.name < "0016":
                    conn.executescript(migration.read_text(encoding="utf-8"))
            stamp = "2026-09-18T00:00:00+00:00"
            conn.execute("INSERT INTO users(id, created_at, updated_at) VALUES(?, ?, ?)", (USER, stamp, stamp))
            conn.executemany(
                "INSERT INTO outreach_targets(id, user_id, company, origin, location, created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?)",
                [
                    ("t-search", USER, "Searched", "discovery", "San Carlos, CA", stamp, stamp),
                    ("t-typed", USER, "Typed", "manual", "Austin, TX", stamp, stamp),
                    ("t-empty", USER, "Empty", "discovery", "", stamp, stamp),
                ],
            )
            conn.executescript(next(m for m in migrations if m.name.startswith("0016")).read_text(encoding="utf-8"))
            bases = dict(conn.execute("SELECT id, location_basis FROM outreach_targets").fetchall())
        finally:
            conn.close()
        self.assertEqual(bases, {"t-search": "research", "t-typed": "manual", "t-empty": ""})


class CompanyDedupeTests(ProfileTestCase):
    def test_names_that_differ_only_in_form_are_one_company(self):
        self.assertEqual(company_key("The Acme Robotics Co."), "acme robotics")
        self.assertEqual(company_key("ACME ROBOTICS, INC."), company_key("Acme Robotics"))
        self.assertEqual(company_key("Hart & Sons"), company_key("Hart and Sons"))
        self.assertNotEqual(company_key("Gaingels Acme Robotics LLC"), company_key("Acme Robotics"))
        self.assertEqual(company_key("Company"), "company", "a name made only of legal words is kept")

    def test_import_and_manual_add_refuse_a_variant_of_a_tracked_company(self):
        create_target(self.conn, {"company": "Acme Robotics", "website": "https://acme.ai"}, user_id=USER)
        result = import_targets(self.conn, [{"company": "Acme Robotics, Inc.", "website": "https://acmerobotics.com"}], user_id=USER)
        self.assertEqual((result["imported"], result["skipped"]), (0, 1))
        with self.assertRaisesRegex(ValueError, "Acme Robotics is already in your outreach list"):
            create_target(self.conn, {"company": "acme robotics inc"}, user_id=USER)

    def test_a_deleted_company_is_remembered_until_it_is_added_back(self):
        target = create_target(self.conn, {"company": "Bovi Robotics", "website": "https://www.bovi.com"}, user_id=USER)
        self.assertTrue(delete_target(self.conn, target["id"], user_id=USER))
        self.assertFalse(delete_target(self.conn, target["id"], user_id=USER))
        row = self.conn.execute("SELECT company_key, domain FROM outreach_dismissed WHERE user_id=?", (USER,)).fetchone()
        self.assertEqual(tuple(row), ("bovi robotics", "bovi.com"))
        create_target(self.conn, {"company": "Bovi Robotics, Inc."}, user_id=USER)
        self.assertIsNone(self.conn.execute("SELECT 1 FROM outreach_dismissed").fetchone(), "adding it back undoes the deletion")


class DiscoveryExclusionTests(ProfileTestCase):
    def setUp(self):
        super().setUp()
        self.sites = {
            host: {"/": f"<p>{name}</p>", "/about": f"<p>About {name}</p>", "/robots.txt": ""}
            for host, name in (
                ("bovi.com", "Bovi"), ("orbit.example", "Orbit Systems"), ("acme.example", "Acme Robotics"),
                ("kite.example", "Kite Labs"), ("newco.example", "Newco"),
            )
        }
        self.prompts = []

    def run_with(self, reply, **kwargs):
        from opportunity_app.outreach_discovery import run_discovery

        answer = only_for(reply)

        def runner(prompt):
            self.prompts.append(prompt)
            return answer(prompt)

        transport, _ = site_transport(self.sites)
        with httpx.Client(transport=transport) as client:
            return run_discovery(
                self.conn, user_id=USER, runner=runner, fetcher=safe_fetcher(client), report_dir=self.root / "reports",
                contact_delay=0, today=TODAY, scopes=["local-accelerators"], **kwargs,
            )

    def test_deleted_and_applied_companies_are_never_proposed(self):
        bovi = create_target(self.conn, {"company": "Bovi", "website": "https://bovi.com"}, user_id=USER)
        delete_target(self.conn, bovi["id"], user_id=USER)
        # The fixture feed has open postings at Acme Robotics and Orbit Systems,
        # and an application at Orbit Systems.
        result = self.run_with(proposals(
            company("Bovi Inc", "https://www.bovi.com"),
            company("Acme Robotics, Inc.", "https://acme.example"),
            company("Orbit Systems", "https://orbit.example"),
            company("Newco", "https://newco.example"),
        ))
        reasons = {item["company"]: item["reason"] for item in result["rejected"]}
        self.assertEqual(reasons["Bovi Inc"], "you deleted it from outreach")
        self.assertEqual(reasons["Orbit Systems"], "you already have an application with them")
        self.assertEqual(result["imported"], 2)
        self.assertIn("## Removed by the student (never propose these)\n- Bovi (bovi.com)", self.prompts[0])

    def test_an_open_posting_does_not_block_a_cold_email(self):
        """Acme Robotics has an open posting in the fixture feed and no application.

        A posting and a cold email are separate doors into the same company, so
        the posting must not keep Acme out of outreach.
        """
        result = self.run_with(proposals(company("Acme Robotics, Inc.", "https://acme.example")))
        self.assertEqual([item["company"] for item in result["rejected"]], [])
        self.assertEqual(result["imported"], 1)
        tracked = self.conn.execute("SELECT company FROM outreach_targets WHERE user_id=?", (USER,)).fetchall()
        self.assertIn("Acme Robotics, Inc.", [row[0] for row in tracked])

    def test_recent_rejections_go_back_into_the_prompt_with_their_reasons(self):
        create_target(self.conn, {"company": "Kite Labs", "website": "https://kite.example"}, user_id=USER)
        self.sites["ghost.example"] = {"/": "<p>Ghost</p>"}
        self.run_with(proposals(
            company("Ghost", "https://ghost.example"),  # its only source 404s
            company("Kite Labs", "https://kite.example"),  # already tracked: nothing to learn
        ))
        self.run_with(proposals())
        self.assertIn("- Ghost: none of its source URLs loaded", self.prompts[1])
        self.assertNotIn("- Kite Labs:", self.prompts[1])


class SiteLocationTests(ProfileTestCase):
    def test_structured_data_outranks_prose_and_street_addresses(self):
        found = site_location([
            page("https://acme.com/", '<script type="application/ld+json">{"@context": "https://schema.org", "@graph": ['
                 '{"@type": "WebSite"}, {"@type": "Organization", "address": {"@type": "PostalAddress", '
                 '"addressLocality": "San Carlos", "addressRegion": "California", "addressCountry": "US"}}]}</script>'
                 "<footer>Visit us at 500 Congress Ave, Austin, TX 78701</footer>"),
        ], company="Acme")
        self.assertEqual(found, {"location": "San Carlos, CA", "source_url": "https://acme.com/"})

    def test_a_stated_headquarters_outranks_other_offices(self):
        found = site_location([
            page("https://acme.com/about", "<p>Acme is headquartered in Round Rock, Texas.</p>"),
            page("https://acme.com/contact", "<p>Sales office: 1 Market St, San Francisco, CA 94105</p>"),
        ], company="Acme")
        self.assertEqual(found, {"location": "Round Rock, TX", "source_url": "https://acme.com/about"})

    def test_a_footer_run_together_with_the_company_name_keeps_only_the_city(self):
        found = site_location([page("https://acme.com/", "<footer>Copyright 2026 Acme Robotics Austin, TX 78701</footer>")], company="Acme Robotics, Inc.")
        self.assertEqual(found["location"], "Austin, TX")

    def test_several_places_at_the_same_level_choose_none(self):
        found = site_location([
            page("https://acme.com/contact", "<p>100 Main St, Austin, TX 78701</p><p>9 Elm St, Boston, MA 02110</p>"),
        ], company="Acme")
        self.assertEqual(found, {"location": "", "ambiguous": ["Austin, TX", "Boston, MA"]})

    def test_a_city_without_a_zip_code_or_headquarters_wording_is_not_an_address(self):
        found = site_location([page("https://acme.com/", "<p>We shipped robots to Denver, CO and Austin, TX.</p>")], company="Acme")
        self.assertEqual(found, {"location": ""})

    def test_find_contacts_records_the_sites_location_without_touching_a_typed_one(self):
        sites = {"acme.com": {
            "/robots.txt": "",
            "/": '<a href="/contact">Contact</a>',
            "/contact": "<p>Acme Robotics</p><p>200 W 5th St, Austin, TX 78701</p>",
        }}
        researched = create_target(self.conn, {"company": "Acme", "website": "https://acme.com"}, user_id=USER, origin="discovery")
        update_target(self.conn, researched["id"], {"location": "Dallas, TX"}, user_id=USER)
        self.conn.execute("UPDATE outreach_targets SET location_basis='research' WHERE id=?", (researched["id"],))
        self.conn.commit()
        transport, _ = site_transport(sites)
        with httpx.Client(transport=transport) as client:
            result = find_contacts(self.conn, researched["id"], user_id=USER, fetcher=safe_fetcher(client), delay=0)
        self.assertEqual(result["location"]["outcome"], "recorded")
        target = get_target(self.conn, researched["id"], user_id=USER)
        self.assertEqual(
            (target["location"], target["location_basis"], target["location_source_url"], target["location_verified"]),
            ("Austin, TX", "company_site", "https://acme.com/contact", True),
        )
        recorded = [event for event in self.events(researched["id"]) if event["event_type"] == "location_recorded"]
        self.assertIn("replaced Dallas, TX from the deep search", recorded[0]["detail"])

        update_target(self.conn, researched["id"], {"location": "Pflugerville, TX"}, user_id=USER)
        with httpx.Client(transport=transport) as client:
            again = find_contacts(self.conn, researched["id"], user_id=USER, fetcher=safe_fetcher(client), delay=0)
        self.assertEqual(again["location"]["outcome"], "kept")
        target = get_target(self.conn, researched["id"], user_id=USER)
        self.assertEqual((target["location"], target["location_basis"]), ("Pflugerville, TX", "manual"))

    def test_a_draft_mentions_being_nearby_only_for_a_checked_location(self):
        facts = {"break_location": "Bay Area", "school": "The University of Texas at Austin"}
        target = create_target(
            self.conn, {"company": "Bovi", "location": "San Carlos, CA"}, user_id=USER, origin="discovery",
        )
        self.assertEqual((target["location_basis"], target["location_verified"]), ("research", False))
        self.assertEqual(location_line(facts, target), "", "the deep search's word alone does not reach an email")
        confirmed = confirm_research(self.conn, target["id"], user_id=USER)
        self.assertTrue(confirmed["location_verified"])
        self.assertIn("Bay Area", location_line(facts, confirmed))


class FormDTests(ProfileTestCase):
    def lookup(self, hits, company_name="Bovi Robotics", **kwargs):
        transport, requested = sec_transport(hits, **kwargs)
        with httpx.Client(transport=transport) as client:
            return form_d_lookup(company_name, fetcher=safe_fetcher(client), today=TODAY, pause=0), requested

    def test_the_latest_filing_by_the_exact_issuer_is_used(self):
        record, requested = self.lookup(BOVI_HITS)
        self.assertIn("q=%22Bovi%20Robotics%22&forms=D", requested[0])
        self.assertTrue(requested[1].endswith("/1995988/000199598826000003/primary_doc.xml"), requested[1])
        self.assertEqual(record["status"], "found")
        self.assertEqual((record["issuer"], record["filed_at"], record["filings"]), ("Bovi Robotics, Inc.", "2026-03-05", 2))
        self.assertEqual(record["location"], "San Carlos, CA")
        self.assertEqual((record["total_sold"], record["total_offering"], record["first_sale"]), (3_200_000, 5_000_000, "2026-03-01"))
        self.assertEqual(record["related_people"], [{"name": "Ada Lovelace", "roles": ["Executive Officer"]}])
        self.assertEqual(record["url"], "https://www.sec.gov/Archives/edgar/data/1995988/000199598826000003/0001995988-26-000003-index.htm")

    def test_two_issuers_with_the_name_are_ambiguous_and_none_is_a_miss(self):
        other = form_d_hit("Bovi Robotics LLC", "0000000042", "0000000042-26-000001", "2026-01-01", "Tulsa, OK")
        record, _ = self.lookup([*BOVI_HITS, other])
        self.assertEqual(record["status"], "ambiguous")
        self.assertEqual({item["location"] for item in record["issuers"]}, {"San Carlos, CA", "Tulsa, OK"})
        record, _ = self.lookup(BOVI_HITS[:1])
        self.assertEqual(record, {"checked_at": "2026-09-18", "status": "none"})

    def test_a_filing_sets_a_missing_location_and_confirms_a_researched_one(self):
        record, _ = self.lookup(BOVI_HITS)
        empty = create_target(self.conn, {"company": "Bovi Robotics"}, user_id=USER)
        self.assertEqual(record_form_d(self.conn, empty["id"], user_id=USER, form_d=record, today=TODAY)["outcome"], "recorded")
        target = get_target(self.conn, empty["id"], user_id=USER)
        self.assertEqual((target["location"], target["location_basis"]), ("San Carlos, CA", "sec_form_d"))
        self.assertEqual(target["sec_form_d"]["total_sold"], 3_200_000)
        self.assertTrue(any(event["event_type"] == "sec_form_d" for event in self.events(empty["id"])))

        researched = create_target(self.conn, {"company": "Bovi Robotics Two", "location": "San Carlos, California"}, user_id=USER, origin="discovery")
        outcome = record_form_d(self.conn, researched["id"], user_id=USER, form_d=record, today=TODAY)
        self.assertEqual(outcome["outcome"], "confirmed")
        self.assertEqual(get_target(self.conn, researched["id"], user_id=USER)["location_verified"], True)

    def test_a_filing_from_another_place_or_long_ago_changes_no_location(self):
        record, _ = self.lookup(BOVI_HITS)
        typed = create_target(self.conn, {"company": "Bovi Robotics", "location": "Tulsa, OK"}, user_id=USER)
        outcome = record_form_d(self.conn, typed["id"], user_id=USER, form_d=record, today=TODAY)
        target = get_target(self.conn, typed["id"], user_id=USER)
        self.assertEqual(outcome["status"], "mismatch")
        self.assertEqual((target["location"], target["sec_form_d"]["mismatch_with"]), ("Tulsa, OK", "Tulsa, OK"))

        old = {**record, "filed_at": "2019-01-01"}
        empty = create_target(self.conn, {"company": "Bovi Robotics Old"}, user_id=USER)
        self.assertEqual(record_form_d(self.conn, empty["id"], user_id=USER, form_d=old, today=TODAY)["outcome"], "stored")
        self.assertEqual(get_target(self.conn, empty["id"], user_id=USER)["location"], "")

    def test_lookups_need_an_identifying_user_agent(self):
        with mock.patch.dict("os.environ", {"PIPELINE_SEC_USER_AGENT": ""}):
            self.assertIsNone(sec_fetcher())
        with mock.patch.dict("os.environ", {"PIPELINE_SEC_USER_AGENT": "no contact address"}):
            self.assertIsNone(sec_fetcher())
        with mock.patch.dict("os.environ", {"PIPELINE_SEC_USER_AGENT": "Jane Student jane@example.com"}):
            fetcher = sec_fetcher()
            self.assertEqual(fetcher.client.headers["User-Agent"], "Jane Student jane@example.com")
            fetcher.client.close()


class EnrichTests(ProfileTestCase):
    def test_backfill_checks_targets_without_a_sourced_location_once_a_month(self):
        sites = {"bovi.com": {"/robots.txt": "", "/": "<p>Bovi Robotics is headquartered in San Carlos, CA.</p>"}}
        bovi = create_target(self.conn, {"company": "Bovi Robotics", "website": "https://bovi.com"}, user_id=USER)
        typed = create_target(self.conn, {"company": "Typed", "website": "https://typed.example", "location": "Austin, TX"}, user_id=USER)
        site_transport_, _ = site_transport(sites)
        sec, requested = sec_transport(BOVI_HITS)
        with httpx.Client(transport=site_transport_) as site_client, httpx.Client(transport=sec) as sec_client:
            kwargs = dict(user_id=USER, site_fetcher=safe_fetcher(site_client), form_d_fetcher=safe_fetcher(sec_client),
                          delay=0, sec_pause=0, today=TODAY)
            first = enrich_targets(self.conn, **kwargs)
            second = enrich_targets(self.conn, **kwargs)
        self.assertEqual((first["missing_location_before"], first["missing_location_after"]), (1, 0))
        checked = {item["company"]: item for item in first["results"]}
        self.assertEqual(checked["Bovi Robotics"]["site"]["outcome"], "recorded")
        self.assertEqual(checked["Bovi Robotics"]["form_d"]["outcome"], "kept", "the site outranks the filing")
        self.assertIsNone(checked["Typed"]["site"], "a typed location is not rechecked against the site")
        self.assertEqual(checked["Typed"]["form_d"]["status"], "none")
        self.assertEqual(second["checked"], 0, "nothing is rechecked within 30 days")
        target = get_target(self.conn, bovi["id"], user_id=USER)
        self.assertEqual((target["location"], target["location_basis"]), ("San Carlos, CA", "company_site"))
        self.assertEqual(target["sec_form_d"]["status"], "found")
        self.assertEqual(get_target(self.conn, typed["id"], user_id=USER)["location"], "Austin, TX")
        self.assertEqual(sum("efts.sec.gov" in url for url in requested), 2)

    def test_the_command_line_backfill_runs_without_network_sources(self):
        from opportunity_app import outreach_cli

        create_target(self.conn, {"company": "Offline"}, user_id=USER)
        self.conn.close()
        with mock.patch("sys.stdout") as stdout:
            self.assertEqual(outreach_cli.main(["--db", str(self.platform_path), "enrich", "--no-sec", "--no-site"]), 0)
        printed = "".join(call.args[0] for call in stdout.write.call_args_list)
        self.assertEqual(json.loads(printed)["missing_location_after"], 1)
        self.conn = connect_product(self.platform_path)


class DiscoveryFollowThroughTests(ProfileTestCase):
    def test_a_new_company_gets_its_sites_location_and_its_form_d(self):
        from opportunity_app.outreach_discovery import run_discovery

        sites = {"bovi.com": {
            "/robots.txt": "",
            "/": '<p>Bovi Robotics</p><a href="/about">About us</a>',
            "/about": "<p>About Bovi Robotics. HQ: 1 Main St, San Carlos, CA 94070</p>",
        }}
        site, _ = site_transport(sites)
        sec, _ = sec_transport(BOVI_HITS)
        with httpx.Client(transport=site) as site_client, httpx.Client(transport=sec) as sec_client:
            with mock.patch("opportunity_app.outreach_discovery.form_d_lookup", wraps=lambda *a, **k: form_d_lookup(*a, **{**k, "pause": 0})):
                result = run_discovery(
                    self.conn, user_id=USER, runner=only_for(proposals(company("Bovi Robotics", "https://bovi.com"))),
                    fetcher=safe_fetcher(site_client), form_d_fetcher=safe_fetcher(sec_client),
                    report_dir=self.root / "reports", contact_delay=0, today=TODAY,
                )
        follow = result["follow_through"][0]
        self.assertEqual((follow["location"]["location"], follow["form_d"]["status"]), ("San Carlos, CA", "found"))
        target = get_target(self.conn, follow["target_id"], user_id=USER)
        self.assertEqual((target["location_basis"], target["location_source_url"]), ("company_site", "https://bovi.com/about"))


class FakeRenderer:
    """Stands in for headless Chromium: the HTML a browser would show for each URL."""

    def __init__(self, pages, unavailable=""):
        self.pages = {url.rstrip("/"): html for url, html in pages.items()}
        self.unavailable = unavailable
        self.rendered = []

    def render(self, url):
        self.rendered.append(url)
        html = self.pages.get(url.rstrip("/"))
        return None if html is None else (url, html)


class InferredLocationTests(ProfileTestCase):
    def test_a_label_run_into_the_city_is_split_off_without_breaking_real_names(self):
        from opportunity_app.outreach_profile import format_location

        found = site_location([
            page("https://acme.com/", "<p><b>Headquarters</b>Austin, TX</p><p><i>Loc</i>Austin, TX</p><p>Austin, TX</p>"),
        ], company="Acme")
        self.assertEqual(found, {"location": "Austin, TX", "source_url": "https://acme.com/", "inferred": True})
        for city in ("McKinney", "DeKalb", "LaGrange", "Center Point"):
            self.assertEqual(format_location(city, "TX", from_prose=True), f"{city}, TX")

    def test_a_site_that_names_one_place_gives_an_inferred_location(self):
        found = site_location([
            page("https://acme.com/", "<footer><p>Austin, TX</p></footer>"),
            page("https://acme.com/careers", "<p>Machinist</p><p>Machinist \u00b7 Austin, Texas</p>"),
        ], company="Acme")
        self.assertEqual(found, {"location": "Austin, TX", "source_url": "https://acme.com/", "inferred": True})
        self.assertEqual(
            site_location([page("https://acme.com/", "<p>Offices: Boston, MA | Austin, TX</p>")], company="Acme"),
            {"location": "", "ambiguous": ["Boston, MA", "Austin, TX"]},
        )
        stated = site_location([page("https://acme.com/", "<p>Headquartered in Round Rock, TX.</p><p>Austin, TX</p>")], company="Acme")
        self.assertEqual(stated, {"location": "Round Rock, TX", "source_url": "https://acme.com/"}, "a stated place is not an inference")

    def test_an_inferred_location_waits_for_the_student(self):
        target = create_target(self.conn, {"company": "Bovi"}, user_id=USER)
        pages = [page("https://bovi.com/", "<p>San Carlos, CA</p>")]
        self.assertEqual(record_site_location(self.conn, target["id"], user_id=USER, pages=pages)["outcome"], "recorded")
        target = get_target(self.conn, target["id"], user_id=USER)
        self.assertEqual(
            (target["location"], target["location_basis"], target["location_inferred"], target["location_verified"]),
            ("San Carlos, CA", "company_site", True, False),
        )
        facts = {"break_location": "Bay Area", "school": "The University of Texas at Austin"}
        self.assertEqual(location_line(facts, target), "", "a draft does not rely on a place the site only names")

        confirmed = update_target(self.conn, target["id"], {"confirm_location": "San Carlos, CA"}, user_id=USER)
        self.assertEqual(
            (confirmed["location_basis"], confirmed["location_inferred"], confirmed["location_verified"]),
            ("company_site", False, True),
        )
        self.assertIn("Bay Area", location_line(facts, confirmed))
        update_target(self.conn, target["id"], {"confirm_location": "San Carlos, CA"}, user_id=USER)
        confirmations = [event for event in self.events(target["id"]) if event["event_type"] == "location_confirmed"]
        self.assertEqual([event["detail"] for event in confirmations], ["You confirmed San Carlos, CA"], "confirming twice logs once")

    def test_confirming_a_deep_search_location_makes_it_the_students(self):
        target = create_target(self.conn, {"company": "Bovi", "location": "Tulsa, OK"}, user_id=USER, origin="discovery")
        confirmed = update_target(self.conn, target["id"], {"confirm_location": "Tulsa, OK"}, user_id=USER)
        self.assertEqual((confirmed["location_basis"], confirmed["location_verified"]), ("manual", True))

    def test_a_stated_place_or_a_filing_outranks_an_inferred_one(self):
        from opportunity_app.outreach_profile import apply_location

        mention = [page("https://bovi.com/", "<p>Austin, TX</p>")]
        target = create_target(self.conn, {"company": "Bovi"}, user_id=USER)
        record_site_location(self.conn, target["id"], user_id=USER, pages=mention)
        outcome = apply_location(
            self.conn, target["id"], user_id=USER, location="Austin, TX", basis="company_site", source_url="https://bovi.com/contact",
        )
        self.assertEqual(outcome, "confirmed")
        self.assertTrue(get_target(self.conn, target["id"], user_id=USER)["location_verified"])
        self.assertEqual(record_site_location(self.conn, target["id"], user_id=USER, pages=mention)["outcome"], "kept")

        filed = create_target(self.conn, {"company": "Filed"}, user_id=USER)
        apply_location(self.conn, filed["id"], user_id=USER, location="Boston, MA", basis="sec_form_d", source_url="https://www.sec.gov/x")
        self.assertEqual(record_site_location(self.conn, filed["id"], user_id=USER, pages=mention)["outcome"], "kept")

        searched = create_target(self.conn, {"company": "Searched", "location": "Dallas, TX"}, user_id=USER, origin="discovery")
        self.assertEqual(record_site_location(self.conn, searched["id"], user_id=USER, pages=mention)["outcome"], "recorded")
        replaced = get_target(self.conn, searched["id"], user_id=USER)
        self.assertEqual((replaced["location"], replaced["location_inferred"]), ("Austin, TX", True))


class RenderFallbackTests(ProfileTestCase):
    SHELL = '<div id="root"></div><script src="/app.js"></script>'

    def enrich(self, sites, renderer):
        from opportunity_app.outreach_profile import enrich_target

        target = create_target(self.conn, {"company": "Shell Robotics", "website": "https://shell.example"}, user_id=USER)
        transport, _ = site_transport(sites)
        with httpx.Client(transport=transport) as client:
            result = enrich_target(
                self.conn, target["id"], user_id=USER, site_fetcher=safe_fetcher(client), form_d_fetcher=None,
                today=TODAY, delay=0, renderer=renderer,
            )
        return result, get_target(self.conn, target["id"], user_id=USER)

    def test_a_site_empty_without_javascript_is_read_again_in_a_browser(self):
        renderer = FakeRenderer({
            "https://shell.example/": '<nav><a href="/contact">Contact</a></nav><p>Shell Robotics</p>',
            "https://shell.example/contact": "<p>Headquartered in Austin, TX.</p>",
        })
        result, target = self.enrich({"shell.example": {"/robots.txt": "", "/": self.SHELL}}, renderer)
        self.assertTrue(result["site"]["rendered"])
        self.assertEqual(
            (target["location"], target["location_basis"], target["location_source_url"]),
            ("Austin, TX", "company_site", "https://shell.example/contact"),
        )

    def test_a_site_with_text_is_not_rendered_and_robots_rules_still_apply(self):
        renderer = FakeRenderer({"https://shell.example/": "<p>Headquartered in Austin, TX.</p>"})
        wordy = "<p>" + "We build careful machines for careful people. " * 10 + "</p>"
        _, target = self.enrich({"shell.example": {"/robots.txt": "", "/": wordy}}, renderer)
        self.assertEqual((renderer.rendered, target["location"]), ([], ""))

        renderer = FakeRenderer({
            "https://shell.example/": '<a href="/contact">Contact</a>',
            "https://shell.example/contact": "<p>Headquartered in Austin, TX.</p>",
        })
        self.conn.execute("DELETE FROM outreach_targets")
        self.conn.commit()
        robots = "User-agent: *\nDisallow: /contact\n"
        _, target = self.enrich({"shell.example": {"/robots.txt": robots, "/": self.SHELL}}, renderer)
        self.assertEqual(renderer.rendered, ["https://shell.example/"])
        self.assertEqual(target["location"], "")

    def test_without_a_browser_the_result_says_why(self):
        renderer = FakeRenderer({}, unavailable="Chromium is not installed")
        result, _ = self.enrich({"shell.example": {"/robots.txt": "", "/": self.SHELL}}, renderer)
        self.assertEqual((result["site"]["rendered"], result["site"]["render_error"]), (False, "Chromium is not installed"))

    def test_the_browser_may_only_reach_public_hosts(self):
        from opportunity_app.outreach_render import request_allowed

        addresses = {"public.example": ["93.184.216.34"], "rebind.example": ["93.184.216.34", "10.0.0.5"], "lan.example": ["192.168.1.2"]}
        cache = {}

        def resolve(host):
            return addresses.get(host, [])

        self.assertTrue(request_allowed("https://public.example/app.js", resolve, cache))
        for url in (
            "http://127.0.0.1:8765/api/v1/outreach", "http://localhost:8765/", "https://lan.example/",
            "https://rebind.example/", "https://unknown.example/", "file:///C:/Windows/win.ini", "ws://public.example/",
        ):
            self.assertFalse(request_allowed(url, resolve, cache), url)

    def test_the_deep_search_follow_through_renders_an_empty_site(self):
        from opportunity_app.outreach_discovery import run_discovery

        sites = {"shell.example": {"/robots.txt": "", "/": self.SHELL, "/about": "<p>About Shell Robotics</p>"}}
        renderer = FakeRenderer({"https://shell.example/": "<footer><p>Buda, Texas</p></footer>"})
        transport, _ = site_transport(sites)
        with httpx.Client(transport=transport) as client:
            result = run_discovery(
                self.conn, user_id=USER, runner=only_for(proposals(company("Shell Robotics", "https://shell.example"))),
                fetcher=safe_fetcher(client), renderer=renderer, report_dir=self.root / "reports", contact_delay=0, today=TODAY,
            )
        location = result["follow_through"][0]["location"]
        self.assertEqual((location["rendered"], location["location"], location["inferred"]), (True, "Buda, TX", True))


if __name__ == "__main__":
    unittest.main()
