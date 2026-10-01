"""An interrupted fetch resumes instead of starting over.

Closing the laptop mid-run suspends the fetch, and on waking the network is not
back for a few seconds, so the sources in flight fail. Those failures must be
told apart from a board that is really gone, and a resumed run must skip the
sources that already arrived.
"""

from __future__ import annotations

import io
import time
import unittest
import unittest.mock
import urllib.error
import urllib.request
from pathlib import Path
from tempfile import TemporaryDirectory

from pipeline_core import fetch as core_fetch, http, paths, sources, store

SOURCES = {
    "discovery_title_terms": ["intern"],
    "ats_sources": [
        {"kind": "greenhouse", "company": "Alpha", "token": "alpha"},
        {"kind": "greenhouse", "company": "Beta", "token": "beta"},
        {"kind": "greenhouse", "company": "Gamma", "token": "gamma"},
    ],
}

try:
    import realdata_guard
except ImportError:  # imported as tests.<module>, with tests/ not on sys.path
    from tests import realdata_guard
# Guards this module's own run against opening data/*.db (AGENTS.md hard rule 1); see tests/realdata_guard.py.
realdata_guard.install()


class TransientErrorTests(unittest.TestCase):
    def test_connection_failures_timeouts_and_server_errors_are_transient(self):
        for error in (
            urllib.error.URLError("getaddrinfo failed"),
            TimeoutError("timed out"),
            ConnectionResetError("reset"),
            urllib.error.HTTPError("https://x", 503, "Unavailable", {}, io.BytesIO()),
            urllib.error.HTTPError("https://x", 429, "Too Many Requests", {}, io.BytesIO()),
        ):
            # repr, not the exception itself: the object is only a label, and
            # pytest-xdist cannot serialise a URLError back to the controller,
            # so passing it directly breaks a parallel run. The assertion below
            # still uses the real exception.
            with self.subTest(error=repr(error)):
                self.assertTrue(http._is_transient(error))

    def test_missing_boards_and_bad_payloads_are_not_transient(self):
        for error in (
            urllib.error.HTTPError("https://x", 404, "Not Found", {}, io.BytesIO()),
            ValueError("bad json"),
            None,
        ):
            with self.subTest(error=repr(error)):
                self.assertFalse(http._is_transient(error))

    def test_http_json_raises_the_transient_type_when_offline(self):
        with unittest.mock.patch.object(
            urllib.request, "urlopen", side_effect=urllib.error.URLError("offline")
        ), unittest.mock.patch.object(time, "sleep"):
            with self.assertRaises(http.TransientFetchError):
                http.request_json("https://boards-api.greenhouse.io/v1/boards/x/jobs")

    def test_http_json_raises_a_plain_error_for_a_404(self):
        not_found = urllib.error.HTTPError("https://x", 404, "Not Found", {}, io.BytesIO())
        # request_json retries a 404 with a real randomised backoff (up to ~4.5 s); the sleep is not what is under test.
        with unittest.mock.patch.object(urllib.request, "urlopen", side_effect=not_found),                 unittest.mock.patch.object(time, "sleep"):
            with self.assertRaises(RuntimeError) as caught:
                http.request_json("https://x")
        self.assertNotIsInstance(caught.exception, http.TransientFetchError)


class ResumeFetchTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        patcher = unittest.mock.patch.object(paths, "DB_PATH", Path(temp.name) / "pipeline.db")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.conn = store.connect()
        self.addCleanup(self.conn.close)

    def fetch(self, behaviour, resume_since=None):
        """Run fetch_all with greenhouse_jobs replaced; return (failures, companies fetched)."""
        fetched = []

        def fake(source, _terms):
            fetched.append(source["company"])
            outcome = behaviour.get(source["company"], [])
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        with unittest.mock.patch.object(sources, "greenhouse_jobs", side_effect=fake), \
                unittest.mock.patch("sys.stdout", io.StringIO()), unittest.mock.patch("sys.stderr", io.StringIO()):
            failures = core_fetch.fetch_all(self.conn, SOURCES, resume_since)
        return failures, fetched

    def test_counts_only_transient_failures(self):
        failures, fetched = self.fetch({
            "Beta": http.TransientFetchError("offline"),
            "Gamma": RuntimeError("HTTP Error 404"),
        })
        # A set, not a list: sources are fetched concurrently, so completion
        # order is not a property of the code under test.
        self.assertEqual(set(fetched), {"Alpha", "Beta", "Gamma"})
        self.assertEqual(failures, 1)

    def test_resume_skips_sources_that_already_succeeded_in_this_run(self):
        started = "2000-01-01T00:00:00Z"
        failures, _ = self.fetch({"Beta": http.TransientFetchError("offline")}, started)
        self.assertEqual(failures, 1)

        failures, fetched = self.fetch({}, started)
        self.assertEqual(fetched, ["Beta"], "only the source that failed is fetched again")
        self.assertEqual(failures, 0)

    def test_successes_from_before_the_run_do_not_count(self):
        self.fetch({})
        failures, fetched = self.fetch({}, "2999-01-01T00:00:00Z")
        self.assertEqual(set(fetched), {"Alpha", "Beta", "Gamma"})
        self.assertEqual(failures, 0)


if __name__ == "__main__":
    unittest.main()
