"""Assets are addressed by content, so caching them forever is safe.

Every page used to reference `/assets/styles.css`, while `index.html` alone
referenced `/assets/styles.css?v=20260918-outreach-split` -- one file, two
URLs, and a version string a human had to remember to bump. Long-lived caching
could not be turned on for either: the unversioned URL would go permanently
stale, and the versioned one would too whenever someone edited the CSS without
editing the string.

The version is now the file's own content hash, so a change to a file changes
its URL and a stale cache entry is unreachable rather than merely unlikely.
"""

from __future__ import annotations

import re
import sys
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers_platform import build_and_migrate
from opportunity_app.api import create_app

PAGES = ("/", "/market", "/connections/oauth/google/callback", "/employer")
ASSET_REFERENCE = re.compile(r"/assets/([A-Za-z0-9._-]+)(?:\?v=([^\"']*))?")


class AssetCachingTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        _, platform_path = build_and_migrate(root)
        # A private copy of the static directory, so a test may edit an asset.
        self.static = root / "static"
        source = Path(__file__).resolve().parent.parent / "opportunity_app" / "static"
        self.static.mkdir()
        for item in source.iterdir():
            if item.is_file():
                (self.static / item.name).write_bytes(item.read_bytes())
        self.app = create_app(
            db_path=platform_path, access_token="tok", static_dir=self.static,
            resume_storage=root / "r", capture_storage=root / "c",
            interview_storage=root / "i", rate_limit_per_minute=100000,
        )

    def references(self, client, page):
        response = client.get(page)
        self.assertEqual(response.status_code, 200, page)
        return ASSET_REFERENCE.findall(response.text)

    def test_every_page_stamps_every_asset_it_references(self):
        with TestClient(self.app) as client:
            for page in PAGES:
                with self.subTest(page=page):
                    found = self.references(client, page)
                    self.assertTrue(found, f"{page} references no assets at all")
                    for name, version in found:
                        self.assertTrue(
                            version, f"{page} references /assets/{name} with no version"
                        )

    def test_the_same_asset_gets_the_same_url_on_every_page(self):
        """styles.css is shared; two URLs for it means two cache entries."""

        with TestClient(self.app) as client:
            seen: dict[str, set[str]] = {}
            for page in PAGES:
                for name, version in self.references(client, page):
                    seen.setdefault(name, set()).add(version)
        self.assertIn("styles.css", seen)
        for name, versions in seen.items():
            with self.subTest(asset=name):
                self.assertEqual(len(versions), 1, f"{name} was served under {versions}")

    def test_an_asset_at_its_current_version_is_cacheable_forever(self):
        with TestClient(self.app) as client:
            name, version = self.references(client, "/")[0]
            response = client.get(f"/assets/{name}?v={version}")
        self.assertEqual(response.status_code, 200)
        self.assertIn("immutable", response.headers["Cache-Control"])
        self.assertIn("max-age=31536000", response.headers["Cache-Control"])

    def test_an_asset_without_a_version_still_revalidates(self):
        with TestClient(self.app) as client:
            response = client.get("/assets/styles.css")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-cache")

    def test_a_stale_version_is_not_cacheable(self):
        """The old hand-written string must not be honoured as current."""

        with TestClient(self.app) as client:
            response = client.get("/assets/styles.css?v=20260918-outreach-split")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-cache")

    def test_editing_an_asset_changes_its_url(self):
        """This is the property that makes immutable caching safe.

        With a hand-maintained string, editing the CSS and forgetting to bump
        it left every browser holding the old file until the string changed.
        """

        with TestClient(self.app) as client:
            before = dict(self.references(client, "/"))["styles.css"]
        target = self.static / "styles.css"
        target.write_bytes(target.read_bytes() + b"\n/* edited */\n")
        with TestClient(self.app) as client:
            after = dict(self.references(client, "/"))["styles.css"]
            # ...and the new URL is the one that is cacheable.
            response = client.get(f"/assets/styles.css?v={after}")
        self.assertNotEqual(before, after, "editing the file left its URL unchanged")
        self.assertIn("immutable", response.headers["Cache-Control"])

    def test_pages_themselves_are_never_cached_immutably(self):
        """A page must revalidate, or a deploy never reaches the browser."""

        with TestClient(self.app) as client:
            for page in PAGES:
                with self.subTest(page=page):
                    response = client.get(page)
                    self.assertEqual(response.headers["Cache-Control"], "no-cache")

    def test_an_unknown_asset_is_not_advertised_as_cacheable(self):
        with TestClient(self.app) as client:
            response = client.get("/assets/does-not-exist.js?v=0")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.headers["Cache-Control"], "no-cache")

    def outside_file(self):
        """A readable file next to, not inside, the static directory."""

        outside = self.static.parent / "outside.txt"
        outside.write_bytes(b"secret outside the asset directory")
        digest = hashlib.sha256(outside.read_bytes()).hexdigest()[:12]
        return outside, digest

    def test_paths_outside_the_static_directory_are_never_hashed_or_cacheable(self):
        """`/assets//<absolute path>` makes pathlib discard static_dir, and
        `%2e%2e` escapes it; neither may confirm a guessed content hash."""

        outside, digest = self.outside_file()
        absolute = outside.resolve().as_posix()
        probes = (
            f"/assets//{absolute}?v={digest}",
            f"/assets/%2e%2e/outside.txt?v={digest}",
            f"/assets/%2e%2e%2foutside.txt?v={digest}",
            f"/assets/..%5coutside.txt?v={digest}",
        )
        with TestClient(self.app) as client:
            for probe in probes:
                with self.subTest(probe=probe):
                    hashed = []
                    real_sha256 = hashlib.sha256

                    def spy(data=b"", *args, **kwargs):
                        hashed.append(data)
                        return real_sha256(data, *args, **kwargs)

                    with mock.patch("opportunity_app.api.hashlib.sha256", spy):
                        response = client.get(probe)
                    self.assertEqual(response.headers["Cache-Control"], "no-cache")
                    self.assertNotIn(outside.read_bytes(), hashed)

    def test_a_revalidation_304_keeps_the_immutable_header(self):
        """A browser updates its stored headers from a 304, so it must not
        downgrade an immutable entry."""

        with TestClient(self.app) as client:
            version = dict(self.references(client, "/"))["styles.css"]
            url = f"/assets/styles.css?v={version}"
            first = client.get(url)
            second = client.get(url, headers={"If-None-Match": first.headers["ETag"]})
        self.assertEqual(second.status_code, 304)
        self.assertIn("immutable", second.headers["Cache-Control"])

    def test_the_version_cache_does_not_grow_with_arbitrary_requests(self):
        """Only names that are real static files may be remembered."""

        real_sha256 = hashlib.sha256
        hashed = []

        def spy(data=b"", *args, **kwargs):
            hashed.append(data)
            return real_sha256(data, *args, **kwargs)

        with TestClient(self.app) as client:
            with mock.patch("opportunity_app.api.hashlib.sha256", spy):
                for index in range(20):
                    client.get(f"/assets/missing-{index}.js?v=0")
                    client.get(f"/assets/missing-{index}.js")
        self.assertEqual(hashed, [])


if __name__ == "__main__":
    unittest.main()
