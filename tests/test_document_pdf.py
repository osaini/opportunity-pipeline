"""Preparation documents as PDF: the Markdown subset, escaping, and the download route."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi.testclient import TestClient

from opportunity_app import STATIC_DIR
from opportunity_app import api as api_module
from opportunity_app.api import create_app
from opportunity_app.document_pdf import markdown_to_html

from helpers_platform import build_and_migrate

LETTER = """# Cover letter — Intern at Acme

Dear Hiring Team,

I am applying for the **Robotics Intern** role. See [my site](https://example.edu/me).

Sincerely,
Test Student
<!-- Target: Intern at Acme; source posting is context, not a profile claim. -->
"""


class MarkdownTests(unittest.TestCase):
    def test_the_forms_these_documents_use(self):
        page = markdown_to_html(LETTER + "\n## Skills\n- Python\n- *CAD*\n", title="Cover letter v1")
        self.assertIn("<h1>Cover letter — Intern at Acme</h1>", page)
        self.assertIn("<strong>Robotics Intern</strong>", page)
        self.assertIn('<a href="https://example.edu/me">my site</a>', page)
        # A letter's sign-off keeps its line break.
        self.assertIn("<p>Sincerely,<br>Test Student</p>", page)
        self.assertIn("<ul><li>Python</li><li><em>CAD</em></li></ul>", page)
        # The generator's note to itself is not printed.
        self.assertNotIn("Target:", page)
        self.assertIn("<title>Cover letter v1</title>", page)

    def test_nothing_in_a_draft_becomes_markup_it_did_not_ask_for(self):
        page = markdown_to_html('<script>alert(1)</script>\n\n[x](javascript:alert(1))\n\n<img src=x onerror=alert(1)>')
        self.assertNotIn("<script>alert", page)
        self.assertNotIn("<img", page)
        self.assertNotIn('href="javascript', page)
        self.assertIn("&lt;script&gt;", page)


class PdfRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        _, self.platform_path = build_and_migrate(self.root)
        self.headers = {"Authorization": "Bearer pdf-owner"}

    def tearDown(self):
        self.tmp.cleanup()

    def client(self, renderer):
        return TestClient(create_app(
            db_path=self.platform_path, access_token="pdf-owner", static_dir=STATIC_DIR,
            resume_storage=self.root / "resumes", capture_storage=self.root / "captures",
            interview_storage=self.root / "interviews", document_pdf_renderer=renderer,
        ))

    def document(self, client):
        created = client.post("/api/v1/preparation/documents", headers=self.headers,
                              json={"opportunity_id": "job-a", "document_type": "cover_letter"})
        self.assertEqual(created.status_code, 201, created.text)
        return created.json()

    def test_the_document_renders_to_a_pdf_download(self):
        pages = []

        def renderer(page_html):
            pages.append(page_html)
            return b"%PDF-1.7 rendered"

        with self.client(renderer) as client:
            record = self.document(client)
            self.assertTrue(client.get("/api/v1/preparation/documents", headers=self.headers).json()["pdf_available"])
            response = client.get(f"/api/v1/preparation/documents/{record['id']}/pdf", headers=self.headers)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.headers["content-type"], "application/pdf")
            self.assertIn(f'filename="cover_letter-v{record["version"]}.pdf"', response.headers["content-disposition"])
            self.assertEqual(response.content, b"%PDF-1.7 rendered")
            self.assertIn("<h1>", pages[0])
            self.assertEqual(client.get("/api/v1/preparation/documents/missing/pdf", headers=self.headers).status_code, 404)

    def test_without_playwright_the_app_says_how_to_add_it(self):
        with mock.patch.object(api_module, "pdf_renderer", return_value=None), self.client(None) as client:
            record = self.document(client)
            self.assertFalse(client.get("/api/v1/preparation/documents", headers=self.headers).json()["pdf_available"])
            response = client.get(f"/api/v1/preparation/documents/{record['id']}/pdf", headers=self.headers)
            self.assertEqual(response.status_code, 503)
            self.assertIn("playwright install chromium", response.json()["detail"])

    def test_a_renderer_failure_is_reported_not_raised(self):
        def broken(_page_html):
            raise RuntimeError("Executable doesn't exist")

        with self.client(broken) as client:
            record = self.document(client)
            response = client.get(f"/api/v1/preparation/documents/{record['id']}/pdf", headers=self.headers)
            self.assertEqual(response.status_code, 503)
            self.assertIn("RuntimeError", response.json()["detail"])


if __name__ == "__main__":
    unittest.main()
