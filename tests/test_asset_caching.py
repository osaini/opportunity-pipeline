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
import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
