"""Outreach settings written to .env from the web app, and in effect without a restart."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi.testclient import TestClient

from opportunity_app import STATIC_DIR
from opportunity_app.api import create_app
from opportunity_app.outreach_gmail import attachment_path
from opportunity_app.outreach_settings import OutreachSettings

from helpers_platform import build_and_migrate
import test_platform

ENV = "# Personal settings\nPIPELINE_WEB_TOKEN=keep-me\nPIPELINE_OUTREACH_PROVIDER=anthropic\n"


class OutreachSettingsApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        _, self.platform_path = build_and_migrate(self.root)
        self.env_path = self.root / ".env"
        self.env_path.write_text(ENV, encoding="utf-8")
        self.attachments = self.root / "attachment"
        environment = mock.patch.dict(os.environ, {
            "PIPELINE_OUTREACH_PROVIDER": "anthropic", "PIPELINE_OUTREACH_DISCOVERY_PROVIDER": "", "PIPELINE_OUTREACH_ATTACHMENT": "",
        })
        environment.start()
        self.addCleanup(environment.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def client(self, settings):
        app = create_app(
            db_path=self.platform_path, access_token="settings-owner", static_dir=STATIC_DIR,
            resume_storage=self.root / "resumes", capture_storage=self.root / "captures",
            interview_storage=self.root / "interviews", outreach_settings=settings,
        )
        return TestClient(app)

    def test_changes_reach_env_and_take_effect_at_once(self):
        settings = OutreachSettings(env_path=self.env_path, attachment_dir=self.attachments, resume_storage=self.root / "resumes")
        headers = {"Authorization": "Bearer settings-owner"}
        with self.client(settings) as client:
            view = client.get("/api/v1/outreach/settings", headers=headers).json()
            self.assertEqual((view["available"], view["draft_provider"]["value"], view["research_agent"]["value"]),
                             (True, "anthropic", "claude-code"))
            self.assertIn("legacy", [option["id"] for option in view["draft_provider"]["options"]])

            uploaded = client.post(
                "/api/v1/resumes", headers=headers,
                files={"resume": ("Test Student Resume.docx", test_platform.PlatformTests.sample_docx(),
                                  "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
            ).json()
            changed = client.put("/api/v1/outreach/settings", headers=headers, json={
                "draft_provider": "legacy", "research_agent": "codex-cli", "attachment_resume_id": uploaded["id"],
            })
            self.assertEqual(changed.status_code, 200, changed.text)
            self.assertEqual(changed.json()["attachment"]["name"], "Test Student Resume.docx")
            self.assertEqual(changed.json()["attachment"]["problem"], "")

            # In effect now, for every reader of the environment.
            self.assertEqual(os.environ["PIPELINE_OUTREACH_PROVIDER"], "legacy")
            self.assertEqual(os.environ["PIPELINE_OUTREACH_DISCOVERY_PROVIDER"], "codex-cli")
            self.assertEqual(attachment_path().name, "Test Student Resume.docx")
            self.assertEqual(attachment_path().read_bytes()[:2], b"PK")

            # And kept for the next start, with the rest of .env untouched.
            text = self.env_path.read_text(encoding="utf-8")
            self.assertIn("# Personal settings", text)
            self.assertIn("PIPELINE_WEB_TOKEN=keep-me", text)
            self.assertIn("PIPELINE_OUTREACH_PROVIDER=legacy", text)
            self.assertNotIn("PIPELINE_OUTREACH_PROVIDER=anthropic", text)

            cleared = client.put("/api/v1/outreach/settings", headers=headers, json={"attachment_resume_id": ""}).json()
            self.assertEqual(cleared["attachment"]["name"], "")
            self.assertIsNone(attachment_path())
            self.assertEqual(os.environ["PIPELINE_OUTREACH_PROVIDER"], "legacy")

            for bad in ({"draft_provider": "gpt-99"}, {"research_agent": "anthropic"}, {"attachment_resume_id": "resume-missing"}):
                self.assertEqual(client.put("/api/v1/outreach/settings", headers=headers, json=bad).status_code, 422, bad)
            self.assertEqual(client.get("/api/v1/outreach/settings").status_code, 401)

    def test_a_scratch_database_never_writes_env(self):
        with self.client(None) as client:
            headers = {"Authorization": "Bearer settings-owner"}
            self.assertEqual(client.get("/api/v1/outreach/settings", headers=headers).json(), {"available": False})
            self.assertEqual(client.put("/api/v1/outreach/settings", headers=headers, json={"draft_provider": "legacy"}).status_code, 409)
        self.assertEqual(self.env_path.read_text(encoding="utf-8"), ENV)


if __name__ == "__main__":
    unittest.main()
