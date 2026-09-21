"""Locations found by a web search, and the page that has to back each one up."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx

from opportunity_app.outreach import create_target, get_target, update_target
from opportunity_app.outreach_locate import clean_place, locate_targets, needs_a_location
from opportunity_app.outreach_profile import apply_location
from opportunity_app.schema import connect_product, ensure_product_schema

from helpers_platform import build_and_migrate
from test_outreach_discovery import safe_fetcher, site_transport

USER = "local-user"

SITES = {
    "ycombinator.com": {
        "/companies/aeon-industrial": "<h1>Aeon Industrial</h1><p>Founded 2025. Cedar Park, TX</p>",
        "/companies/queue": "<h1>Queue</h1><p>Queue builds kitchen robots in Oakland, CA</p>",
    },
    "techcrunch.example": {
        "/kara-seed": "<p>Kara Labs raised a seed round. The company is based in Boston, MA.</p>",
    },
    "aeonindustrial.com": {"/": "<p>Aeon Industrial</p>"},
}


def reply(*companies):
    return "Here is what I found:\n" + json.dumps({"companies": list(companies)})


def found(company, location, source_url, note=""):
    return {"company": company, "location": location, "source_url": source_url, "note": note}


class LocateTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        _, platform_path = build_and_migrate(Path(self.tempdir.name))
        self.conn = connect_product(platform_path)
        ensure_product_schema(self.conn)
        self.prompts = []

    def tearDown(self):
        self.conn.close()
        self.tempdir.cleanup()

    def add(self, company, website="", **fields):
        return create_target(self.conn, {"company": company, "website": website, **fields}, user_id=USER)

    def run_with(self, raw, **kwargs):
        def runner(prompt):
            self.prompts.append(prompt)
            return raw

        transport, _ = site_transport(SITES)
        with httpx.Client(transport=transport) as client:
            return locate_targets(self.conn, user_id=USER, runner=runner, fetcher=safe_fetcher(client), **kwargs)

    def location_of(self, target_id):
        target = get_target(self.conn, target_id, user_id=USER)
        return (target["location"], target["location_basis"], target["location_source_url"], target["location_verified"])

    def test_a_location_its_source_page_states_is_recorded_and_usable(self):
        target = self.add("Aeon Industrial", "https://www.aeonindustrial.com")
        result = self.run_with(reply(found("Aeon Industrial", "cedar park, tx", "https://ycombinator.com/companies/aeon-industrial")))
        self.assertEqual(
            self.location_of(target["id"]),
            ("Cedar Park, TX", "web_search", "https://ycombinator.com/companies/aeon-industrial", True),
        )
        self.assertEqual((result["searched"], result["recorded"], result["missing_location_after"]), (1, 1, 0))
        self.assertIn("Aeon Industrial (https://www.aeonindustrial.com)", self.prompts[0])

    def test_a_source_that_does_not_state_the_place_or_the_company_is_refused(self):
        wrong_place = self.add("Aeon Industrial", "https://www.aeonindustrial.com")
        self.run_with(reply(found("Aeon Industrial", "Austin, TX", "https://ycombinator.com/companies/aeon-industrial")))
        self.assertEqual(self.location_of(wrong_place["id"])[0], "", "the page states Cedar Park, not Austin")

        other_company = self.add("Queue", "https://queue.inc")
        result = self.run_with(reply(found("Queue", "Boston, MA", "https://techcrunch.example/kara-seed")))
        self.assertEqual(self.location_of(other_company["id"])[0], "")
        reasons = {item["company"]: item["reason"] for item in result["results"]}
        self.assertEqual(reasons["Queue"], "its source does not mention the company")

    def test_a_dateline_states_a_city_but_a_different_state_contradicts_one(self):
        SITES["press.example"] = {
            "/gritt": "<p>SAN FRANCISCO, July 21, 2026. Gritt launched today.</p>",
            "/steinmetz": "<p>Steinmetz opened its Austin, MN plant.</p>",
        }
        self.addCleanup(SITES.pop, "press.example", None)
        dateline = self.add("Gritt", "https://gritt.ai")
        self.run_with(reply(found("Gritt", "San Francisco, CA", "https://press.example/gritt")))
        self.assertEqual(self.location_of(dateline["id"])[:2], ("San Francisco, CA", "web_search"),
                         "a dateline names the city; not naming the state is not a contradiction")

        wrong_state = self.add("Steinmetz", "https://steinmetzmotors.com")
        result = self.run_with(reply(found("Steinmetz", "Austin, TX", "https://press.example/steinmetz")))
        self.assertEqual(self.location_of(wrong_state["id"])[0], "", "the page puts Austin in Minnesota")
        self.assertIn("does not place it in Austin, TX", result["results"][0]["reason"])

    def test_a_dead_private_or_missing_source_records_nothing(self):
        target = self.add("Kara", "https://karalabs.ai")
        cases = {
            "https://ycombinator.com/companies/gone": "did not load",
            "http://127.0.0.1:8000/kara": "not a public http(s) page",
            "": "not a public http(s) page",
        }
        for url, reason in cases.items():
            result = self.run_with(reply(found("Kara", "Boston, MA", url)))
            self.assertEqual(self.location_of(target["id"])[0], "", url)
            self.assertIn(reason, result["results"][0]["reason"], url)

    def test_an_empty_or_unparsable_answer_is_reported_not_stored(self):
        target = self.add("Steinmetz", "https://steinmetzmotors.com")
        empty = self.run_with(reply(found("Steinmetz", "", "", note="No page names a city")))
        self.assertEqual(empty["results"][0]["reason"], "no page states where it is based")
        self.assertEqual(empty["results"][0]["note"], "No page names a city")

        vague = self.run_with(reply(found("Steinmetz", "the Bay Area", "https://ycombinator.com/companies/queue")))
        self.assertIn("is not a city and state", vague["results"][0]["reason"])
        self.assertEqual(self.location_of(target["id"])[0], "")

    def test_a_company_the_search_skipped_or_invented_is_reported(self):
        skipped = self.add("Theseus", "https://theseus.us")
        result = self.run_with(reply(found("Someone Else", "Austin, TX", "https://ycombinator.com/companies/queue")))
        self.assertEqual(result["results"], [{
            "target_id": skipped["id"], "company": "Theseus", "location": "", "source_url": "",
            "note": "", "reason": "the search did not answer for it", "outcome": "refused",
        }])

    def test_only_targets_nothing_better_placed_are_searched(self):
        typed = self.add("Aeon Industrial", "https://www.aeonindustrial.com", location="Cedar Park, TX")
        from_site = self.add("Contoro Robotics", "https://contoro.com")
        apply_location(self.conn, from_site["id"], user_id=USER, location="Austin, TX",
                       basis="company_site", source_url="https://contoro.com/contact")
        inferred = self.add("Sorcerer", "https://sorcerer.earth")
        apply_location(self.conn, inferred["id"], user_id=USER, location="San Francisco, CA",
                       basis="company_site", source_url="https://sorcerer.earth/", inferred=True)
        blank = self.add("Queue", "https://queue.inc")

        result = self.run_with(reply(
            found("Sorcerer", "Oakland, CA", "https://ycombinator.com/companies/queue"),
            found("Queue", "Oakland, CA", "https://ycombinator.com/companies/queue"),
        ))
        searched = {item["company"] for item in result["results"]}
        self.assertEqual(searched, {"Sorcerer", "Queue"}, "a typed or site-stated location is left alone")
        self.assertEqual(self.location_of(typed["id"])[1], "manual")
        self.assertEqual(self.location_of(from_site["id"])[1], "company_site")
        self.assertEqual(self.location_of(blank["id"])[:2], ("Oakland, CA", "web_search"))

    def test_a_searched_page_replaces_a_place_the_site_only_mentioned(self):
        target = self.add("Queue", "https://queue.inc")
        apply_location(self.conn, target["id"], user_id=USER, location="Hershey, PA",
                       basis="company_site", source_url="https://queue.inc/", inferred=True)
        self.assertFalse(get_target(self.conn, target["id"], user_id=USER)["location_verified"])
        self.run_with(reply(found("Queue", "Oakland, CA", "https://ycombinator.com/companies/queue")))
        self.assertEqual(self.location_of(target["id"]), ("Oakland, CA", "web_search", "https://ycombinator.com/companies/queue", True))

    def test_the_students_own_entry_survives_a_search(self):
        target = self.add("Queue", "https://queue.inc")
        update_target(self.conn, target["id"], {"location": "Palo Alto, CA"}, user_id=USER)
        result = self.run_with(reply(found("Queue", "Oakland, CA", "https://ycombinator.com/companies/queue")),
                               target_ids=[target["id"]])
        self.assertEqual(result["results"][0]["outcome"], "kept")
        self.assertEqual(self.location_of(target["id"])[:2], ("Palo Alto, CA", "manual"))

    def test_companies_are_searched_in_batches(self):
        for name in ("Queue", "Kara", "Theseus"):
            self.add(name, f"https://{name.lower()}.example")
        self.run_with(reply(), batch_size=2)
        self.assertEqual(len(self.prompts), 2)
        self.assertEqual(self.prompts[0].count(".example)"), 2)
        self.assertEqual(self.prompts[1].count(".example)"), 1)

    def test_clean_place_and_needs_a_location(self):
        self.assertEqual(clean_place("cedar park, tx"), "Cedar Park, TX")
        self.assertEqual(clean_place("San Francisco, California"), "San Francisco, CA")
        self.assertEqual(clean_place("Berlin, Germany"), "Berlin, Germany")
        self.assertEqual(clean_place("Remote"), "")
        self.assertTrue(needs_a_location({"location": "", "location_basis": "", "location_inferred": 0}))
        self.assertTrue(needs_a_location({"location": "Austin, TX", "location_basis": "research", "location_inferred": 0}))
        self.assertTrue(needs_a_location({"location": "Austin, TX", "location_basis": "company_site", "location_inferred": 1}))
        self.assertFalse(needs_a_location({"location": "Austin, TX", "location_basis": "sec_form_d", "location_inferred": 0}))
        self.assertFalse(needs_a_location({"location": "Austin, TX", "location_basis": "manual", "location_inferred": 0}))

    def test_a_location_nothing_established_is_still_due_for_checking(self):
        """An import file's word, and any basis this code does not know, stay open.

        Naming the open bases one by one is what let an unrecognised value be
        skipped here while counting as unverified everywhere else: never relied
        on, and never checked either.
        """
        for basis in ("", "research", "a_basis_from_the_future"):
            with self.subTest(basis=basis):
                self.assertTrue(needs_a_location(
                    {"location": "Austin, TX", "location_basis": basis, "location_inferred": 0}
                ))
        for basis in ("company_site", "sec_form_d", "web_search"):
            with self.subTest(basis=basis):
                row = {"location": "Austin, TX", "location_basis": basis, "location_inferred": 0}
                self.assertFalse(needs_a_location(row), "a page established it")
                self.assertTrue(needs_a_location({**row, "location_inferred": 1}), "only inferred from it")


if __name__ == "__main__":
    unittest.main()
