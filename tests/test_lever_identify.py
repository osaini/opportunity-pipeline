"""Which saved role is a Lever posting (apply/lever.py): identify, the application page's address, the sender check.

docs/phase5-lever-handoff-spec.md 5.3. No browser, no network, and nothing in the app calls this module yet. Every site and
posting here is fictional; the uuids are made up.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from opportunity_app.apply import lever
from opportunity_app.apply.greenhouse import identify as identify_greenhouse
from opportunity_app.apply.lever import ATS_LEVER, ADAPTER_VERSION, LEVER_HOSTS, LeverRef, canonical_url, identify, is_lever_sender
from opportunity_app.core.timestamps import utc_now

from helpers_apply import ApplyCase
# unittest and pytest run the module fixtures they find in the test module's namespace.
from helpers_apply import setUpModule, tearDownModule  # noqa: F401
from helpers_source import PACKAGE_DIR, apply_modules

JOB = "5b1f3d2e-7a4c-4e8b-9d60-1c2e3f4a5b6c"
OTHER_JOB = "0a9b8c7d-6e5f-4a3b-8c2d-1e0f9a8b7c6d"
EU = "jobs.eu.lever.co"
GLOBAL = "jobs.lever.co"


class IdentifyTests(ApplyCase):
    def role(self, opportunity_id, url, sources=()):
        self.opportunity(opportunity_id)
        with self.conn:
            self.conn.execute("UPDATE opportunities SET url=? WHERE id=?", (url, opportunity_id))
            for key, external_id, source_url in sources:
                self.conn.execute(
                    "INSERT INTO opportunity_sources(opportunity_id, source_key, source_name, external_id, source_url, first_seen_at, last_seen_at) "
                    "VALUES(?, ?, 'x', ?, ?, ?, ?)",
                    (opportunity_id, key, external_id, source_url, utc_now(), utc_now()))
        return opportunity_id

    def identified(self, url, sources=()):
        self.serial += 1
        return identify(self.conn, self.role(f"r{self.serial}", url, sources))

    def test_the_url_names_the_site_the_posting_and_the_host_on_either_lever_host(self):
        for host in LEVER_HOSTS:
            with self.subTest(host=host):
                self.assertEqual(self.identified(f"https://{host}/examplerobotics/{JOB}"), LeverRef("examplerobotics", JOB, host))

    def test_an_apply_or_thanks_path_a_closing_slash_a_query_or_a_fragment_may_follow_the_uuid(self):
        for tail in ("/apply", "/thanks", "/", "/apply/", "?lever-source=Board", "/apply?lever-origin=applied&lever-source=Board", "#section", "/thanks#top"):
            with self.subTest(tail=tail):
                self.assertEqual(self.identified(f"https://{GLOBAL}/examplerobotics/{JOB}{tail}"), LeverRef("examplerobotics", JOB, GLOBAL))

    def test_an_http_link_is_the_same_posting(self):
        self.assertEqual(self.identified(f"http://{GLOBAL}/examplerobotics/{JOB}"), LeverRef("examplerobotics", JOB, GLOBAL))

    def test_the_host_is_matched_whatever_its_case_or_a_trailing_dot(self):
        self.assertEqual(self.identified(f"https://JOBS.Lever.CO./examplerobotics/{JOB}"), LeverRef("examplerobotics", JOB, GLOBAL))

    def test_the_eu_host_is_kept_so_an_eu_posting_stays_on_it(self):
        found = self.identified(f"https://{EU}/eusite/{JOB}/apply")
        self.assertEqual(found.host, EU)
        self.assertEqual(canonical_url(*found), f"https://{EU}/eusite/{JOB}/apply")

    def test_a_uuid_that_is_not_lowercase_hex_in_the_8_4_4_4_12_shape_is_refused(self):
        for bad in ("1", "not-a-uuid", JOB.upper(), JOB[:-1], JOB + "0", JOB.replace("-", ""), JOB[:-1] + "g", f"{JOB}/extra", f"{JOB}%0a"):
            with self.subTest(job=bad):
                self.assertIsNone(self.identified(f"https://{GLOBAL}/examplerobotics/{bad}"))

    def test_a_site_with_a_character_outside_letters_digits_underscore_and_hyphen_is_refused(self):
        for site in ("exa mple", "exa.mple", "exa%20mple", "exa:mple", "exam$ple", "x" * 101, ""):
            with self.subTest(site=site):
                self.assertIsNone(self.identified(f"https://{GLOBAL}/{site}/{JOB}"))
        self.assertIsNotNone(self.identified(f"https://{GLOBAL}/{'x' * 100}/{JOB}"))
        self.assertIsNotNone(self.identified(f"https://{GLOBAL}/Example_Site-9/{JOB}"))

    def test_nothing_but_apply_or_thanks_may_follow_the_uuid_in_the_path(self):
        for tail in ("/apply/extra", "/other", "/apply/thanks", "//", "/thanks/x", "/applyx"):
            with self.subTest(tail=tail):
                self.assertIsNone(self.identified(f"https://{GLOBAL}/examplerobotics/{JOB}{tail}"))

    def test_the_site_page_and_a_path_with_no_posting_are_not_a_posting(self):
        for url in (f"https://{GLOBAL}/examplerobotics", f"https://{GLOBAL}/examplerobotics/", f"https://{GLOBAL}/", f"https://{GLOBAL}"):
            with self.subTest(url=url):
                self.assertIsNone(self.identified(url))

    def test_lookalike_hosts_credentials_ports_and_other_schemes_are_not_lever(self):
        for url in (
            f"https://jobs.lever.co.evil.example.test/acme/{JOB}", f"https://evil-jobs.lever.co/acme/{JOB}", f"https://lever.co/acme/{JOB}",
            f"https://hire.lever.co/acme/{JOB}", f"https://api.lever.co/acme/{JOB}", f"https://user:pass@{GLOBAL}/acme/{JOB}",
            f"https://{GLOBAL}:8443/acme/{JOB}", f"https://jobs.lever.co@evil.example.test/acme/{JOB}", f"ftp://{GLOBAL}/acme/{JOB}",
            f"https://example.test/jobs.lever.co/acme/{JOB}", f"https://example.test/acme/{JOB}?next=https://{GLOBAL}/acme/{JOB}",
        ):
            with self.subTest(url=url):
                self.assertIsNone(self.identified(url))

    def test_a_company_site_that_embeds_lever_is_not_a_lever_posting_unless_its_url_names_lever(self):
        self.assertIsNone(self.identified(f"https://careers.example.test/jobs?lever-source=x&id={JOB}"))
        self.assertIsNone(self.identified("https://careers.example.test/jobs/1", [("greenhouse:acme", JOB, f"https://careers.example.test/jobs/{JOB}")]))

    def test_a_greenhouse_posting_is_not_lever_and_a_lever_posting_is_not_greenhouse(self):
        self.assertIsNone(self.identified("https://job-boards.greenhouse.io/examplerobotics/jobs/4000000001"))
        lever_role = self.role("lv1", f"https://{GLOBAL}/examplerobotics/{JOB}/apply")
        self.assertIsNone(identify_greenhouse(self.conn, lever_role))
        self.assertIsNotNone(identify(self.conn, lever_role))

    def test_a_source_url_is_read_when_the_role_url_is_the_companys_own(self):
        found = self.identified("https://careers.example.test/jobs/1", [("lever:acme", OTHER_JOB, f"https://{EU}/acmeeu/{JOB}/apply")])
        self.assertEqual(found, LeverRef("acmeeu", JOB, EU), "the URL decides the site, the posting and the host, not the source key")

    def test_the_role_url_wins_over_a_source_url_and_a_source_url_over_the_source_key(self):
        own = self.identified(f"https://{GLOBAL}/urlsite/{JOB}", [("lever:keysite", OTHER_JOB, f"https://{GLOBAL}/sourcesite/{OTHER_JOB}")])
        self.assertEqual(own, LeverRef("urlsite", JOB, GLOBAL))
        by_source_url = self.identified("https://careers.example.test/jobs/2", [("lever:keysite", OTHER_JOB, f"https://{GLOBAL}/sourcesite/{JOB}")])
        self.assertEqual(by_source_url, LeverRef("sourcesite", JOB, GLOBAL))

    def test_the_source_key_and_external_id_are_used_when_no_url_names_lever_and_take_the_global_host(self):
        found = self.identified("https://careers.example.test/jobs/3", [("lever:acme", JOB, "https://careers.example.test/jobs/3")])
        self.assertEqual(found, LeverRef("acme", JOB, GLOBAL))

    def test_a_source_key_with_a_bad_site_or_a_bad_external_id_is_refused(self):
        for key, external_id in (("lever:ex ample", JOB), ("lever:", JOB), ("lever:acme", "12345"), ("lever:acme", ""), ("lever:acme", JOB.upper()),
                                 ("lever:acme", f"{JOB}\n"), ("lever:acme.co", JOB)):
            with self.subTest(key=key, external_id=external_id):
                self.assertIsNone(self.identified("https://careers.example.test/jobs/4", [(key, external_id, "https://careers.example.test/jobs/4")]))

    def test_another_ats_source_key_is_not_a_lever_source_row(self):
        for key in ("ashby:acme", "greenhouse:acme", "workday:acme", "levers:acme", "xlever:acme"):
            with self.subTest(key=key):
                self.assertIsNone(self.identified("https://careers.example.test/jobs/5", [(key, JOB, "https://careers.example.test/jobs/5")]))

    def test_the_newest_source_row_is_read_first(self):
        self.opportunity("multi")
        with self.conn:
            self.conn.execute("UPDATE opportunities SET url='https://careers.example.test/jobs/6' WHERE id='multi'")
            for site, job, seen in (("older", OTHER_JOB, "2026-01-01T00:00:00+00:00"), ("newer", JOB, "2026-02-01T00:00:00+00:00")):
                self.conn.execute(
                    "INSERT INTO opportunity_sources(opportunity_id, source_key, source_name, external_id, source_url, first_seen_at, last_seen_at) "
                    "VALUES('multi', ?, 'x', ?, '', ?, ?)", (f"lever:{site}", job, seen, seen))
        self.assertEqual(identify(self.conn, "multi"), LeverRef("newer", JOB, GLOBAL))

    def test_an_unknown_role_is_none(self):
        self.assertIsNone(identify(self.conn, "no-such-role"))

    def test_a_role_with_no_url_and_no_sources_is_none(self):
        self.assertIsNone(identify(self.conn, self.role("bare", "")))


class ConstantsTests(unittest.TestCase):
    def test_the_names_and_hosts_the_spec_pins(self):
        self.assertEqual((ATS_LEVER, ADAPTER_VERSION), ("lever", "lever-1"))
        self.assertEqual(LEVER_HOSTS, ("jobs.lever.co", "jobs.eu.lever.co"))

    def test_the_application_page_is_the_posting_with_apply_on_the_end_and_defaults_to_the_global_host(self):
        self.assertEqual(canonical_url("acme", JOB, EU), f"https://{EU}/acme/{JOB}/apply")
        self.assertEqual(canonical_url("acme", JOB), f"https://{GLOBAL}/acme/{JOB}/apply")
        self.assertEqual(canonical_url("acme", JOB, GLOBAL), canonical_url(*LeverRef("acme", JOB, GLOBAL)))

    def test_what_identify_returns_can_be_turned_back_into_the_same_posting(self):
        for host in LEVER_HOSTS:
            url = canonical_url("acme", JOB, host)
            self.assertEqual(lever._from_url(url), LeverRef("acme", JOB, host))

    def test_lever_senders_are_lever_and_its_subdomains_only(self):
        for domain in ("hire.lever.co", "lever.co", "HIRE.LEVER.CO", "hire.lever.co.", "mail.hire.lever.co", "jobs.lever.co"):
            with self.subTest(domain=domain):
                self.assertTrue(is_lever_sender(domain))
        for domain in ("", None, "notlever.co", "lever.co.evil.example.test", "hire-lever.co", "greenhouse.io", "lever.com", "evil.example.test"):
            with self.subTest(domain=domain):
                self.assertFalse(is_lever_sender(domain))

    def test_the_sender_domains_are_the_ones_the_application_inbox_rules_already_list(self):
        import json
        listed = json.loads((PACKAGE_DIR / "mail" / "data" / "application_senders.json").read_text(encoding="utf-8"))
        senders = set(listed if isinstance(listed, list) else next(value for value in listed.values() if isinstance(value, list)))
        self.assertTrue(set(lever.LEVER_SENDER_DOMAINS) <= senders)


class SourceGuardTests(unittest.TestCase):
    def test_the_source_key_pattern_is_a_parameter_and_not_part_of_the_sql(self):
        # A literal % in the SQL breaks on PostgreSQL, where ? becomes %s (the same rule greenhouse.identify keeps).
        text = apply_modules()["apply/lever.py"]
        self.assertNotRegex(text, r"LIKE\s+'")
        self.assertIn('"lever:%"', text)

    def test_lever_py_imports_no_first_party_module(self):
        text = apply_modules()["apply/lever.py"]
        self.assertNotIn("opportunity_app", text.replace("opportunity_app/", ""))
        self.assertNotRegex(text, r"^\s*from \.", "greenhouse.py's rule: the request rules import it, so it imports nothing of ours")


if __name__ == "__main__":
    unittest.main()
