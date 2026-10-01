"""pypdf and Pillow load when a PDF or a screenshot is read, not when the app imports.

Importing pypdf costs about 100 ms and Pillow about 25 ms, and every process that imports opportunity_app.api (each
test, the CLI, the server's start) paid for both. The error handling that names their exceptions has to keep working
with the import moved inside the functions.
"""

import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from opportunity_app import ROOT
from opportunity_app.captures import CaptureValidationError, _scan_image
from opportunity_app.resumes import ResumeValidationError, extract_pdf, extract_pdf_links

from helpers_platform import sample_docx  # noqa: F401  (installs the real-data guard for a single-module run)


def modules_after_importing(*names):
    """Which of pypdf, PIL and playwright's probe are loaded after importing ``names`` in a fresh interpreter."""
    code = (
        "import sys; sys.path.insert(0, %r); import %s; "
        "print(' '.join(sorted(m for m in ('pypdf', 'PIL') if m in sys.modules)))" % (str(ROOT), ", ".join(names))
    )
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=ROOT, check=True)
    return done.stdout.split()


class LazyImportTests(unittest.TestCase):
    def test_resumes_and_captures_import_without_pypdf_or_pillow(self):
        self.assertEqual(modules_after_importing("opportunity_app.resumes", "opportunity_app.captures"), [])

    def test_the_api_imports_without_them_too(self):
        self.assertEqual(modules_after_importing("opportunity_app.api"), [])

    def test_a_bad_pdf_is_still_a_validation_error(self):
        with self.assertRaises(ResumeValidationError) as caught:
            extract_pdf(b"%PDF-1.4 this is not a pdf")
        self.assertEqual(str(caught.exception), "The PDF could not be read")
        self.assertEqual(extract_pdf_links(b"%PDF-1.4 not a real pdf"), [])

    def test_a_bad_screenshot_is_still_a_validation_error(self):
        with self.assertRaises(CaptureValidationError) as caught:
            _scan_image(b"not an image at all")
        self.assertEqual(str(caught.exception), "The screenshot could not be read")

    def test_the_malware_test_string_is_still_refused_first(self):
        with self.assertRaises(CaptureValidationError) as caught:
            _scan_image(b"EICAR-STANDARD-ANTIVIRUS-TEST-FILE")
        self.assertIn("malware", str(caught.exception))

    def test_a_real_png_is_still_accepted(self):
        import io

        from PIL import Image

        buffer = io.BytesIO()
        Image.new("RGB", (4, 4), "white").save(buffer, "PNG")
        self.assertEqual(_scan_image(buffer.getvalue()), "image/png")
        buffer = io.BytesIO()
        Image.new("RGB", (4, 4), "white").save(buffer, "GIF")
        with self.assertRaises(CaptureValidationError):
            _scan_image(buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
