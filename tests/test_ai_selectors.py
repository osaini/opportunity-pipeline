"""Each AI feature picks its own model, from what this computer can run, and says so when it cannot."""

import os
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi.testclient import TestClient

from opportunity_app import STATIC_DIR, agent_providers
from opportunity_app.api import DocumentCreateRequest, create_app
from opportunity_app.outreach import create_target
from opportunity_app.outreach_drafting import generate_draft, resolve_provider
from opportunity_app.outreach_review import review_choice, review_runner
from opportunity_app.outreach_settings import OutreachSettings
from opportunity_app.schema import connect_product

from helpers_platform import build_and_migrate

AUTH = {"Authorization": "Bearer selector-owner"}
USER = "local-user"
CLEAN = {
    "PIPELINE_OUTREACH_PROVIDER": "", "PIPELINE_OUTREACH_FOLLOW_UP_PROVIDER": "", "PIPELINE_OUTREACH_CALL_PREP_PROVIDER": "",
    "PIPELINE_OUTREACH_REVIEW_PROVIDER": "", "PIPELINE_OUTREACH_DISCOVERY_PROVIDER": "",
}


def catalog(*ready):
    """A provider catalog where only ``ready`` are set up."""
    names = {"openai": "OpenAI", "anthropic": "Anthropic", "claude-code": "Claude Code (subscription)", "codex-cli": "Codex CLI (subscription)"}
    return [
        {"id": provider, "display_name": name, "model": f"{provider}-model", "configured": provider in ready,
         "setup_hint": "" if provider in ready else f"Set up {name}."}
        for provider, name in names.items()
    ]


class WriterTests(unittest.TestCase):
    def test_follow_ups_and_call_prep_follow_the_draft_writer_unless_set(self):
        with mock.patch.dict(os.environ, {**CLEAN, "PIPELINE_OUTREACH_PROVIDER": "anthropic"}):
            self.assertEqual(resolve_provider(None, purpose="follow_up")[0], "anthropic")
            self.assertEqual(resolve_provider(None, purpose="call_prep")[0], "anthropic")
            os.environ["PIPELINE_OUTREACH_FOLLOW_UP_PROVIDER"] = "legacy"
            os.environ["PIPELINE_OUTREACH_CALL_PREP_PROVIDER"] = "codex-cli"
            self.assertEqual(resolve_provider(None, purpose="follow_up"), ("legacy", ""))
            self.assertEqual(resolve_provider(None, purpose="call_prep")[0], "codex-cli")
            self.assertEqual(resolve_provider(None, purpose="initial")[0], "anthropic", "first emails keep their own writer")
            self.assertEqual(resolve_provider("openai", purpose="follow_up")[0], "openai", "an explicit pick wins")

    def test_a_follow_up_is_written_by_its_own_writer(self):
        with tempfile.TemporaryDirectory() as root, mock.patch.dict(os.environ, {
            **CLEAN, "PIPELINE_OUTREACH_PROVIDER": "anthropic", "PIPELINE_OUTREACH_FOLLOW_UP_PROVIDER": "legacy",
        }):
            _, path = build_and_migrate(Path(root))
            with closing(connect_product(path)) as conn:
                target = create_target(conn, {
                    "company": "Bovi", "contact_email": "greg@bovi.example", "status": "sent",
                    "email_subject": "Hello", "email_body": "Hi Greg,\n\nA note.\n\nSam",
                }, user_id=USER)

                def no_model(_provider, _model):
                    raise AssertionError("the follow-up writer is the template, so no model is built")

                drafted = generate_draft(conn, target["id"], user_id=USER, provider_factory=no_model, kind="follow_up")
                self.assertEqual(drafted["follow_up_generated_by"], "template")


class ReviewerTests(unittest.TestCase):
    def choose(self, ready, **env):
        with mock.patch.dict(os.environ, {**CLEAN, **env}), mock.patch.object(agent_providers, "provider_catalog", return_value=catalog(*ready)):
            return review_choice()

    def test_automatic_picks_a_different_company_than_the_writer(self):
        both = ("claude-code", "codex-cli")
        self.assertEqual(self.choose(both, PIPELINE_OUTREACH_FOLLOW_UP_PROVIDER="claude-code"), ("codex-cli", ""))
        self.assertEqual(self.choose(both, PIPELINE_OUTREACH_FOLLOW_UP_PROVIDER="codex-cli"), ("claude-code", ""))
        self.assertEqual(self.choose(("anthropic", "openai"), PIPELINE_OUTREACH_PROVIDER="openai"), ("anthropic", ""),
                         "API keys count as much as subscriptions")

    def test_with_one_company_set_up_it_uses_that_and_says_so(self):
        provider, note = self.choose(("claude-code",), PIPELINE_OUTREACH_PROVIDER="claude-code")
        self.assertEqual(provider, "claude-code")
        self.assertIn("same family", note)

    def test_a_pick_that_is_not_set_up_or_nothing_set_up_is_refused(self):
        with self.assertRaisesRegex(ValueError, "not set up: Set up OpenAI"):
            self.choose(("claude-code",), PIPELINE_OUTREACH_REVIEW_PROVIDER="openai")
        with self.assertRaisesRegex(ValueError, "No model is set up"):
            self.choose(())
        self.assertEqual(self.choose(("claude-code", "codex-cli"), PIPELINE_OUTREACH_REVIEW_PROVIDER="claude-code",
                                     PIPELINE_OUTREACH_PROVIDER="claude-code"), ("claude-code", ""), "the student's pick stands")

    def test_an_api_provider_reviews_through_a_plain_completion(self):
        class Agent:
            def complete_text(self, instructions, content):
                return '{"send": true, "away_until": null, "problems": []}'

        with mock.patch.dict(os.environ, {**CLEAN, "PIPELINE_OUTREACH_PROVIDER": "openai"}), \
                mock.patch.object(agent_providers, "provider_catalog", return_value=catalog("openai", "anthropic")), \
                mock.patch.object(agent_providers, "build_provider", return_value=Agent()) as built:
            name, run = review_runner()
            self.assertEqual(name, "anthropic")
            self.assertIn('"send": true', run("review this"))
            built.assert_called_once_with("anthropic", "anthropic-model")


class SettingsTests(unittest.TestCase):
    def test_every_writer_and_the_reviewer_can_be_chosen(self):
        with tempfile.TemporaryDirectory() as root, mock.patch.dict(os.environ, CLEAN), \
                mock.patch.object(agent_providers, "provider_catalog", return_value=catalog("claude-code", "codex-cli")):
            root = Path(root)
            _, path = build_and_migrate(root)
            env_path = root / ".env"
            env_path.write_text("PIPELINE_WEB_TOKEN=keep-me\n", encoding="utf-8")
            settings = OutreachSettings(env_path=env_path, attachment_dir=root / "attachment", resume_storage=root / "resumes")
            app = create_app(db_path=path, access_token="selector-owner", static_dir=STATIC_DIR, resume_storage=root / "resumes",
                             capture_storage=root / "captures", interview_storage=root / "interviews", outreach_settings=settings)
            with TestClient(app) as client:
                view = client.get("/api/v1/outreach/settings", headers=AUTH).json()
                self.assertEqual((view["follow_up_provider"]["value"], view["call_prep_provider"]["value"]), ("", ""))
                self.assertIn("legacy", [option["id"] for option in view["call_prep_provider"]["options"]])
                self.assertNotIn("legacy", [option["id"] for option in view["review_provider"]["options"]], "a template cannot review")
                self.assertEqual(view["review_provider"]["automatic"]["id"], "codex-cli")
                saved = client.put("/api/v1/outreach/settings", headers=AUTH, json={
                    "follow_up_provider": "legacy", "call_prep_provider": "codex-cli", "review_provider": "claude-code",
                })
                self.assertEqual(saved.status_code, 200, saved.text)
                self.assertEqual(os.environ["PIPELINE_OUTREACH_FOLLOW_UP_PROVIDER"], "legacy")
                self.assertEqual(os.environ["PIPELINE_OUTREACH_CALL_PREP_PROVIDER"], "codex-cli")
                self.assertEqual(os.environ["PIPELINE_OUTREACH_REVIEW_PROVIDER"], "claude-code")
                text = env_path.read_text(encoding="utf-8")
                self.assertIn("PIPELINE_OUTREACH_REVIEW_PROVIDER=claude-code", text)
                self.assertIn("PIPELINE_WEB_TOKEN=keep-me", text)
                for key, value in (("review_provider", "legacy"), ("follow_up_provider", "gpt-9")):
                    refused = client.put("/api/v1/outreach/settings", headers=AUTH, json={key: value})
                    self.assertEqual(refused.status_code, 422, (key, refused.text))


class DocumentProviderTests(unittest.TestCase):
    def test_the_preparation_page_can_use_a_subscription(self):
        for provider in ("openai", "anthropic", "claude-code", "codex-cli"):
            with self.subTest(provider=provider):
                self.assertEqual(DocumentCreateRequest(opportunity_id="job-a", document_type="resume", provider=provider).provider, provider)


if __name__ == "__main__":
    unittest.main()
