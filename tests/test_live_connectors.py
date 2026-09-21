"""Live-connector code paths: sandbox connect, signed webhook ingest, OCR degrade."""

import hashlib
import hmac
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi.testclient import TestClient

from opportunity_app import STATIC_DIR
from opportunity_app.api import create_app
from opportunity_app.captures import ocr_image
from opportunity_app.schema import LOCAL_USER_ID, connect_product

from helpers_platform import build_and_migrate

WEBHOOK_SECRET = "test-webhook-secret"


class LiveConnectorFlowTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        _, self.platform_path = build_and_migrate(Path(self.tempdir.name))
        self.app = create_app(
            db_path=self.platform_path,
            access_token="connector-flow",
            static_dir=STATIC_DIR,
        )
        self.headers = {"Authorization": "Bearer connector-flow"}
        # Keep one long-lived TestClient; lifespan startup runs on first use.
        self.client = TestClient(self.app)
        created = self.client.post(
            "/api/v1/connections",
            headers=self.headers,
            json={"provider": "sandbox"},
        )
        self.assertEqual(created.status_code, 201, created.text)
        self.connector_id = created.json()["id"]

    def tearDown(self):
        if hasattr(self, "client"):
            self.client.close()
        self.tempdir.cleanup()

    def test_signed_webhook_ingests_event_for_connector_owner(self):
        payload = (
            '{"connector_id": "%s", "external_id": "msg-1",'
            ' "subject": "Application confirmation for Mechanical Internship",'
            ' "body": "Your application was received.", "sender": "careers@example.com"}' % self.connector_id
        )
        signature = "sha256=" + hmac.new(WEBHOOK_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
        with mock.patch.dict("os.environ", {"PIPELINE_WEBHOOK_SECRET": WEBHOOK_SECRET}, clear=False):
            response = self.client.post(
                "/api/v1/connections/webhook",
                content=payload.encode(),
                headers={**self.headers, "Content-Type": "application/json", "X-Webhook-Signature": signature},
            )
        self.assertEqual(response.status_code, 201, response.text)
        event = response.json()
        self.assertEqual(event["status"], "pending")
        self.assertEqual(event["event_type"], "application_confirmation")

        listed = self.client.get("/api/v1/monitored-events", headers=self.headers)
        self.assertEqual(listed.json()["total"], 1)

    def test_webhook_rejects_bad_signature(self):
        payload = '{"connector_id": "%s", "external_id": "msg-2"}' % self.connector_id
        bad = "sha256=" + "0" * 64
        with mock.patch.dict("os.environ", {"PIPELINE_WEBHOOK_SECRET": WEBHOOK_SECRET}, clear=False):
            response = self.client.post(
                "/api/v1/connections/webhook",
                content=payload.encode(),
                headers={**self.headers, "Content-Type": "application/json", "X-Webhook-Signature": bad},
            )
        self.assertEqual(response.status_code, 401)

    def test_unknown_connector_in_webhook_is_404(self):
        payload = '{"connector_id": "connector-google-nobody", "external_id": "msg-3"}'
        signature = "sha256=" + hmac.new(WEBHOOK_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
        with mock.patch.dict("os.environ", {"PIPELINE_WEBHOOK_SECRET": WEBHOOK_SECRET}, clear=False):
            response = self.client.post(
                "/api/v1/connections/webhook",
                content=payload.encode(),
                headers={**self.headers, "Content-Type": "application/json", "X-Webhook-Signature": signature},
            )
        self.assertEqual(response.status_code, 404)


class OcrDegradeTests(unittest.TestCase):
    def test_ocr_returns_empty_without_engine(self):
        from PIL import Image
        import io

        buffer = io.BytesIO()
        Image.new("RGB", (40, 20), "white").save(buffer, format="PNG")
        with mock.patch("opportunity_app.captures.shutil.which", return_value=None):
            self.assertEqual(ocr_image(buffer.getvalue()), "")

    def test_ocr_failure_degrades_to_empty_string(self):
        from PIL import Image
        import io

        buffer = io.BytesIO()
        Image.new("RGB", (40, 20), "white").save(buffer, format="PNG")
        with mock.patch("opportunity_app.captures.shutil.which", return_value="tesseract-fake"):
            with mock.patch("opportunity_app.captures.subprocess.run", side_effect=OSError("boom")):
                self.assertEqual(ocr_image(buffer.getvalue()), "")


if __name__ == "__main__":
    unittest.main()
