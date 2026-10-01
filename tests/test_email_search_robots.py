"""The email search reads a site's robots.txt once per batch, and every verdict stays what it was.

check_person built its own hop guard, and with it a fresh robots.txt cache, for every proposed person, so a model that
cited the same news site for four people fetched its robots.txt four times. search_batch now builds one guard for the
batch and passes it in; check_person without one still builds its own.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx

from opportunity_app.outreach import create_target
from opportunity_app.outreach_email_search import check_person, hop_guard, search_emails
from opportunity_app.schema import connect_product

from helpers_outreach import USER, safe_fetcher, site_transport
from helpers_platform import build_and_migrate

SITES = {
    "news.test": {
        "/robots.txt": "User-agent: *\nDisallow: /private\n",
        "/a": "<p>Jane Doe, jane@acme.test</p>",
        "/b": "<p>Sam Lee, sam@acme.test</p>",
        "/c": "<p>Ann Roe, ann@globex.test</p>",
        "/private/d": "<p>Bob Poe, bob@globex.test</p>",
    },
    "other.test": {"/robots.txt": "", "/e": "<p>Cy Doe, cy@globex.test</p>"},
}


def person(name, email, url):
    return {"name": name, "role": "CTO", "email": email, "source_url": url}


REPLY = json.dumps({"companies": [
    {"company": "Acme", "people": [person("Jane Doe", "jane@acme.test", "https://news.test/a"),
                                   person("Sam Lee", "sam@acme.test", "https://news.test/b")]},
    {"company": "Globex", "people": [person("Ann Roe", "ann@globex.test", "https://news.test/c"),
                                     person("Bob Poe", "bob@globex.test", "https://news.test/private/d"),
                                     person("Cy Doe", "cy@globex.test", "https://other.test/e")]},
]})


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        _, platform_path = build_and_migrate(Path(self.tmp.name))
        self.conn = connect_product(platform_path)
        self.addCleanup(self.conn.close)
        self.ids = [create_target(self.conn, {"company": name, "website": f"https://{name.lower()}.test"}, user_id=USER)["id"]
                    for name in ("Acme", "Globex")]

    def search(self):
        transport, requested = site_transport(SITES)
        with httpx.Client(transport=transport) as client:
            found = search_emails(self.conn, user_id=USER, runner=lambda prompt: REPLY, fetcher=safe_fetcher(client), target_ids=self.ids)
        return found, requested


class RobotsOncePerBatchTests(Case):
    def test_each_sites_robots_is_fetched_once_for_the_batch(self):
        found, requested = self.search()
        robots = [url for url in requested if url.endswith("/robots.txt")]
        self.assertEqual(sorted(robots), ["https://news.test/robots.txt", "https://other.test/robots.txt"])
        self.assertEqual(found["found"], 4)

    def test_the_verdicts_are_the_same_as_checking_each_person_alone(self):
        found, _ = self.search()
        by_company = {result["company"]: result for result in found["results"]}
        self.assertEqual(by_company["Acme"]["kept"], ["jane@acme.test", "sam@acme.test"])
        self.assertEqual(sorted(by_company["Globex"]["kept"]), ["ann@globex.test", "cy@globex.test"])
        self.assertEqual([item["email"] for item in by_company["Globex"]["refused"]], ["bob@globex.test"])
        self.assertIn("robots.txt", by_company["Globex"]["refused"][0]["reason"])
        # Each person on their own, each with a fresh guard, as before.
        transport, _ = site_transport(SITES)
        with httpx.Client(transport=transport) as client:
            fetcher = safe_fetcher(client)
            alone = {}
            for answer in json.loads(REPLY)["companies"]:
                target = {"id": "t", "company": answer["company"], "website": f"https://{answer['company'].lower()}.test"}
                alone[answer["company"]] = [check_person(item, target, fetcher=fetcher)["reason"] for item in answer["people"]]
        self.assertEqual(alone["Acme"], ["", ""])
        self.assertEqual([reason == "" for reason in alone["Globex"]], [True, False, True])

    def test_check_person_without_a_guard_still_fetches_robots_for_itself(self):
        transport, requested = site_transport(SITES)
        target = {"id": "t", "company": "Acme", "website": "https://acme.test"}
        with httpx.Client(transport=transport) as client:
            fetcher = safe_fetcher(client)
            check_person(person("Jane Doe", "jane@acme.test", "https://news.test/a"), target, fetcher=fetcher)
            check_person(person("Sam Lee", "sam@acme.test", "https://news.test/b"), target, fetcher=fetcher)
        self.assertEqual(len([url for url in requested if url.endswith("/robots.txt")]), 2)

    def test_a_shared_guard_is_used_when_given(self):
        transport, requested = site_transport(SITES)
        target = {"id": "t", "company": "Acme", "website": "https://acme.test"}
        with httpx.Client(transport=transport) as client:
            fetcher = safe_fetcher(client)
            guard = hop_guard(fetcher)
            first = check_person(person("Jane Doe", "jane@acme.test", "https://news.test/a"), target, fetcher=fetcher, hop_check=guard)
            second = check_person(person("Sam Lee", "sam@acme.test", "https://news.test/b"), target, fetcher=fetcher, hop_check=guard)
        self.assertEqual((first["reason"], second["reason"]), ("", ""))
        self.assertEqual(len([url for url in requested if url.endswith("/robots.txt")]), 1)

    def test_a_failed_robots_read_is_not_remembered_so_a_later_person_asks_again(self):
        # robots.txt fails once (a 503), then comes back with a Disallow. Checked person by person, the second
        # person is refused; a guard that remembered the failure as "allow everything" would let them through.
        robots_calls = []

        def handler(request):
            if request.url.path == "/robots.txt":
                robots_calls.append(1)
                if len(robots_calls) == 1:
                    return httpx.Response(503)
                return httpx.Response(200, text="User-agent: *\nDisallow: /private\n", headers={"content-type": "text/plain"})
            return httpx.Response(200, text="<p>Bob Poe, bob@acme.test</p>", headers={"content-type": "text/html"})

        target = {"id": "t", "company": "Acme", "website": "https://acme.test"}
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            fetcher = safe_fetcher(client)
            guard = hop_guard(fetcher)
            self.assertIsNone(guard("https://news.test/private/x"))
            self.assertEqual(guard("https://news.test/private/x"), "robots")
            self.assertEqual(guard("https://news.test/private/x"), "robots")
        self.assertEqual(len(robots_calls), 2, "the settled answer is reused")

    def test_a_missing_robots_file_is_settled_and_read_once(self):
        calls = []

        def handler(request):
            calls.append(request.url.path)
            return httpx.Response(404)

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            guard = hop_guard(safe_fetcher(client))
            for _ in range(3):
                self.assertIsNone(guard("https://news.test/private/x"))
        self.assertEqual(calls, ["/robots.txt"])


if __name__ == "__main__":
    unittest.main()
