"""The two Lever recordings (tests/fixtures/apply/lever/endpoints.json and search_locations_reply.json) load and keep their shape.

A later RoutePolicy test reads endpoints.json for the hosts and paths Lever's policy may allow. This test only checks what a
reader can rely on: every entry names a host and a path, a host is a bare name (no scheme, no path, no port), every entry says
whether it was seen, and the lookup reply holds fictional values in the shape the page reads.
"""

import json
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import realdata_guard

realdata_guard.install()

LEVER = Path(__file__).resolve().parent / "fixtures" / "apply" / "lever"
HOST = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")
CATEGORIES = ("document_hosts", "static_asset_hosts", "lookup_endpoints", "upload_endpoints", "captcha_endpoints", "refused_hosts")


def load(name: str):
    return json.loads((LEVER / name).read_text(encoding="utf-8"))


class EndpointsFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = load("endpoints.json")

    def entries(self):
        for key in CATEGORIES:
            for entry in self.data[key]:
                yield key, entry

    def test_dated_and_every_category_present(self):
        self.assertRegex(self.data["checked_on"], r"^\d{4}-\d{2}-\d{2}$")
        for key in (*CATEGORIES, "cloudflare"):
            self.assertTrue(self.data[key], key)

    def test_every_host_is_a_bare_name_and_every_entry_says_if_it_was_seen(self):
        for key, entry in self.entries():
            with self.subTest(key=key, host=entry["host"]):
                self.assertRegex(entry["host"], HOST)
                self.assertIsInstance(entry["confirmed"], bool)

    def test_every_path_starts_with_a_slash(self):
        for key, entry in self.entries():
            for field in ("path", "path_prefix"):
                if field in entry:
                    self.assertTrue(entry[field].startswith("/"), (key, entry))
            for prefix in entry.get("path_prefixes", []):
                self.assertTrue(prefix.startswith("/"), (key, entry))

    def test_lookup_is_the_pair_on_both_lever_hosts_and_points_at_a_real_reply_fixture(self):
        lookups = {(e["host"], e["path_prefix"]) for e in self.data["lookup_endpoints"]}
        self.assertEqual(lookups, {("jobs.lever.co", "/searchLocations"), ("jobs.eu.lever.co", "/searchLocations")})
        for entry in self.data["lookup_endpoints"]:
            self.assertEqual(entry["method"], "GET")
            self.assertTrue((LEVER / entry["reply_fixture"]).is_file())

    def test_the_lever_document_hosts_are_the_lookup_and_cloudflare_hosts(self):
        documents = {e["host"] for e in self.data["document_hosts"]}
        self.assertEqual(documents, {"jobs.lever.co", "jobs.eu.lever.co"})
        self.assertEqual(documents, {e["host"] for e in self.data["lookup_endpoints"]})
        self.assertEqual(documents, set(self.data["cloudflare"]["hosts"]))

    def test_resume_upload_is_one_exact_path_by_post(self):
        for entry in self.data["upload_endpoints"]:
            self.assertEqual((entry["path"], entry["method"]), ("/parseResume", "POST"))

    def test_hcaptcha_hosts_include_the_checked_in_pair_and_the_script_host(self):
        hosts = {e["host"] for e in self.data["captcha_endpoints"]}
        self.assertLessEqual({"hcaptcha.com", "api.hcaptcha.com", "js.hcaptcha.com"}, hosts)
        for entry in self.data["captcha_endpoints"]:
            self.assertTrue(entry["methods"])
            self.assertLessEqual(set(entry["methods"]), {"GET", "POST"})

    def test_shard_pattern_matches_a_shard_and_nothing_else(self):
        shard = re.compile(self.data["captcha_shard_pattern"])
        self.assertTrue(shard.fullmatch("0123456789ab.w.hcaptcha.com"))
        self.assertFalse(shard.fullmatch("0123456789ab.w.hcaptcha.com.example.org"))
        self.assertFalse(shard.fullmatch("w.hcaptcha.com"))
        self.assertFalse(shard.fullmatch("0123456789ab.hcaptcha.com"))

    def test_cloudflare_paths_are_under_cdn_cgi_and_hold_no_token(self):
        cloudflare = self.data["cloudflare"]
        paths = [e["path"] for e in cloudflare["get_paths"]] + [e["prefix"] for e in cloudflare["post_path_prefixes"]]
        self.assertTrue(paths)
        for path in paths:
            self.assertTrue(path.startswith("/cdn-cgi/"), path)
            self.assertNotRegex(path, r"\d\.\d{6,}|:\d{6,}")

    def test_no_refused_host_is_also_allowed(self):
        allowed = {e["host"] for key, e in self.entries() if key != "refused_hosts"}
        for entry in self.data["refused_hosts"]:
            self.assertNotIn(entry["host"], allowed)

    def test_no_websocket_and_one_subframe_host(self):
        self.assertEqual(self.data["websockets_seen"], [])
        self.assertEqual(self.data["subframe_hosts_seen"], ["newassets.hcaptcha.com"])


class SearchLocationsReplyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = load("search_locations_reply.json")

    def test_request_matches_the_pinned_lookup(self):
        request = self.data["request"]
        endpoints = load("endpoints.json")["lookup_endpoints"]
        self.assertIn((request["host"], request["path"]), {(e["host"], e["path_prefix"]) for e in endpoints})
        self.assertEqual(request["method"], "GET")
        self.assertEqual(request["query_keys"], ["text"])
        self.assertEqual(self.data["status"], 200)
        self.assertTrue(self.data["content_type"].startswith("application/json"))

    def test_body_is_a_list_of_name_and_id_with_fictional_values(self):
        body = self.data["body"]
        self.assertIsInstance(body, list)
        self.assertTrue(body)
        for option in body:
            self.assertEqual(set(option), {"name", "id"})
            self.assertIsInstance(option["name"], str)
            self.assertIsInstance(option["id"], str)
            self.assertEqual(len(option["id"]), 40)
            self.assertNotIn("Austin", option["name"])   # the city typed while recording
            self.assertRegex(option["id"], r"^0{39}[1-9]$")   # visibly made up: zeros, then a counter
        self.assertEqual(len({o["id"] for o in body}), len(body))


if __name__ == "__main__":
    unittest.main()
