"""Outbound requests ask for gzip, and undo whatever comes back.

urllib does not decompress on its own. It sets Accept-Encoding only when it
intends to handle the result itself, so asking for gzip by hand means owning
the decode -- and a server is free to ignore the request and answer identity,
so the decision has to be made from the response's Content-Encoding rather
than from what was asked for.

Paginated boards (Workday, USAJOBS, Adzuna) send megabytes of JSON per run,
which is what this is for. It does not change the number of requests.
"""

from __future__ import annotations

import gzip
import io
import json
import unittest
import unittest.mock
import urllib.error
import zlib

import pipeline


class FakeHeaders(dict):
    """Enough of email.message.Message for the code under test."""

    def __init__(self, mapping=None, charset="utf-8"):
        super().__init__(mapping or {})
        self._charset = charset

    def get(self, key, default=None):
        for name, value in self.items():
            if name.lower() == str(key).lower():
                return value
        return default

    def get_content_charset(self):
        return self._charset


class FakeResponse(io.BytesIO):
    def __init__(self, body: bytes, headers: dict | None = None, status: int = 200, url: str = "https://x"):
        super().__init__(body)
        self.headers = FakeHeaders(headers or {})
        self.status = status
        self.url = url

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def respond(body: bytes, headers: dict | None = None, **kwargs):
    return unittest.mock.patch.object(
        pipeline.urllib.request, "urlopen",
        return_value=FakeResponse(body, headers, **kwargs),
    )


PAYLOAD = {"jobs": [{"id": 1, "title": "Mechanical Engineering Intern"}]}


class RequestedEncodingTests(unittest.TestCase):
    def test_both_helpers_ask_for_gzip(self):
        captured = {}

        def capture(request, timeout=None):
            captured[request.full_url] = dict(request.header_items())
            return FakeResponse(b"{}", {"Content-Type": "application/json"})

        with unittest.mock.patch.object(pipeline.urllib.request, "urlopen", side_effect=capture):
            pipeline.request_json("https://example.com/json")
            pipeline.request_text("https://example.com/page")

        for url, headers in captured.items():
            with self.subTest(url=url):
                lowered = {key.lower(): value for key, value in headers.items()}
                self.assertEqual(lowered.get("Accept-encoding".lower()), "gzip")


class JsonDecodeTests(unittest.TestCase):
    def test_a_gzip_body_is_decompressed(self):
        body = gzip.compress(json.dumps(PAYLOAD).encode())
        with respond(body, {"Content-Encoding": "gzip"}):
            self.assertEqual(pipeline.request_json("https://x"), PAYLOAD)

    def test_an_identity_body_is_passed_through(self):
        """A server may ignore Accept-Encoding, and most small ones do."""

        with respond(json.dumps(PAYLOAD).encode(), {}):
            self.assertEqual(pipeline.request_json("https://x"), PAYLOAD)

    def test_an_explicit_identity_encoding_is_passed_through(self):
        with respond(json.dumps(PAYLOAD).encode(), {"Content-Encoding": "identity"}):
            self.assertEqual(pipeline.request_json("https://x"), PAYLOAD)

    def test_a_deflate_body_is_decompressed(self):
        body = zlib.compress(json.dumps(PAYLOAD).encode())
        with respond(body, {"Content-Encoding": "deflate"}):
            self.assertEqual(pipeline.request_json("https://x"), PAYLOAD)

    def test_the_header_is_matched_case_insensitively(self):
        body = gzip.compress(json.dumps(PAYLOAD).encode())
        with respond(body, {"content-encoding": "GZIP"}):
            self.assertEqual(pipeline.request_json("https://x"), PAYLOAD)

    def test_a_body_that_lies_about_being_gzip_fails_the_request(self):
        """Better a failed source than a source silently returning nothing."""

        with respond(b"this is not gzip", {"Content-Encoding": "gzip"}), \
                unittest.mock.patch.object(pipeline.time, "sleep"):
            with self.assertRaises(Exception) as caught:
                pipeline.request_json("https://x")
        self.assertNotIsInstance(caught.exception, SystemExit)

    def test_valid_gzip_holding_invalid_json_still_raises(self):
        with respond(gzip.compress(b"{not json"), {"Content-Encoding": "gzip"}), \
                unittest.mock.patch.object(pipeline.time, "sleep"):
            with self.assertRaises(Exception):
                pipeline.request_json("https://x")


class TextDecodeTests(unittest.TestCase):
    def test_a_gzip_page_is_decompressed(self):
        with respond(gzip.compress("Apply now".encode()), {"Content-Encoding": "gzip"}):
            status, _, body = pipeline.request_text("https://x")
        self.assertEqual((status, body), (200, "Apply now"))

    def test_an_identity_page_is_passed_through(self):
        with respond("Apply now".encode(), {}):
            status, _, body = pipeline.request_text("https://x")
        self.assertEqual((status, body), (200, "Apply now"))

    def test_an_error_pages_compressed_body_still_reaches_the_caller(self):
        """classify_liveness reads the body of a 404 to decide what happened."""

        error = urllib.error.HTTPError(
            "https://x", 404, "Not Found",
            FakeHeaders({"Content-Encoding": "gzip"}),
            io.BytesIO(gzip.compress(b"This posting has closed.")),
        )
        with unittest.mock.patch.object(pipeline.urllib.request, "urlopen", side_effect=error):
            status, _, body = pipeline.request_text("https://x")
        self.assertEqual(status, 404)
        self.assertIn("closed", body)

    def test_an_undecodable_error_body_leaves_the_status_usable(self):
        """A broken body must not abort a liveness check that the status answers."""

        error = urllib.error.HTTPError(
            "https://x", 404, "Not Found",
            FakeHeaders({"Content-Encoding": "gzip"}),
            io.BytesIO(b"not gzip at all"),
        )
        with unittest.mock.patch.object(pipeline.urllib.request, "urlopen", side_effect=error):
            status, _, body = pipeline.request_text("https://x")
        self.assertEqual(status, 404)
        self.assertEqual(body, "")


class CurlFallbackTests(unittest.TestCase):
    """The curl path exists for machines whose trust store Python cannot see.

    curl neither requests nor decodes compression without --compressed, so
    without it the fallback would ask for gzip through urllib and then quietly
    stop asking for it the moment the fallback engaged.
    """

    CERT_FAILURE = urllib.error.URLError("CERTIFICATE_VERIFY_FAILED: unable to get issuer")

    def run_with_curl(self, call, stdout):
        captured = {}

        def fake_run(command, **kwargs):
            captured["command"] = command
            return unittest.mock.Mock(stdout=stdout, returncode=0)

        with unittest.mock.patch.object(pipeline.urllib.request, "urlopen", side_effect=self.CERT_FAILURE), \
                unittest.mock.patch.object(pipeline.subprocess, "run", side_effect=fake_run), \
                unittest.mock.patch.object(pipeline.time, "sleep"):
            result = call()
        return result, captured["command"]

    def test_the_json_fallback_asks_curl_to_decompress(self):
        result, command = self.run_with_curl(
            lambda: pipeline.request_json("https://x"), json.dumps(PAYLOAD)
        )
        self.assertEqual(result, PAYLOAD)
        self.assertIn("--compressed", command)

    def test_the_text_fallback_asks_curl_to_decompress(self):
        (status, _, body), command = self.run_with_curl(
            lambda: pipeline.request_text("https://x"), "Apply now\n200\thttps://x"
        )
        self.assertEqual((status, body.strip()), (200, "Apply now"))
        self.assertIn("--compressed", command)


if __name__ == "__main__":
    unittest.main()
