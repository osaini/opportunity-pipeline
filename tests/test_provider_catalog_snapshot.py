"""The provider catalog is built once per Outreach settings load, and every answer stays what it was.

provider_catalog() scans PATH twice (one shutil.which per CLI). default_provider() used to build it once per
candidate, and one settings load built it more than ten times. agent_providers.catalog_snapshot() makes the first
build in a block serve the rest of it; these tests pin that nothing else changed.
"""

import contextlib
import itertools
import os
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from opportunity_app.integrations import agent_providers
from opportunity_app.outreach.settings import OutreachSettings
from opportunity_app.core.database import connect_product

from helpers_platform import build_and_migrate

USER = "local-user"
PROVIDER_ENV = (
    "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_AGENT_MODEL", "ANTHROPIC_AGENT_MODEL", "CLAUDE_AGENT_MODEL", "CODEX_AGENT_MODEL",
    "PIPELINE_CLAUDE_BIN", "PIPELINE_CODEX_BIN", "PIPELINE_OUTREACH_PROVIDER", "PIPELINE_OUTREACH_REVIEW_PROVIDER",
    "PIPELINE_OUTREACH_FOLLOW_UP_PROVIDER", "PIPELINE_OUTREACH_CALL_PREP_PROVIDER", "PIPELINE_OUTREACH_THANK_YOU_PROVIDER",
)


def clean_env(**extra):
    env = {key: value for key, value in os.environ.items() if key not in PROVIDER_ENV}
    env.update(extra)
    return mock.patch.dict(os.environ, env, clear=True)


class Which:
    """shutil.which stand-in: knows which CLIs are installed and counts every PATH scan."""

    def __init__(self, *installed):
        self.installed = set(installed)
        self.calls = 0

    def __call__(self, binary, *args, **kwargs):
        self.calls += 1
        return f"/bin/{binary}" if binary in self.installed else None


def old_default_provider():
    """default_provider as it was: configured_provider for each candidate, each one building its own catalog."""
    for candidate in ("openai", "anthropic", "claude-code", "codex-cli"):
        try:
            agent_providers.configured_provider(candidate)
            return candidate
        except ValueError:
            continue
    return "legacy"


class CatalogSnapshotTests(unittest.TestCase):
    def test_each_call_scans_path_twice_outside_a_snapshot(self):
        which = Which()
        with clean_env(), mock.patch.object(agent_providers.shutil, "which", which):
            agent_providers.provider_catalog()
            agent_providers.provider_catalog()
        self.assertEqual(which.calls, 4)

    def test_a_snapshot_scans_once_however_many_times_it_is_asked(self):
        which = Which("claude")
        with clean_env(), mock.patch.object(agent_providers.shutil, "which", which):
            with agent_providers.catalog_snapshot():
                first = agent_providers.provider_catalog()
                for _ in range(10):
                    self.assertEqual(agent_providers.provider_catalog(), first)
                self.assertEqual(which.calls, 2)

    def test_a_snapshot_that_is_never_asked_scans_nothing(self):
        which = Which()
        with clean_env(), mock.patch.object(agent_providers.shutil, "which", which):
            with agent_providers.catalog_snapshot():
                pass
        self.assertEqual(which.calls, 0)

    def test_every_answer_is_its_own_copy(self):
        with clean_env(), mock.patch.object(agent_providers.shutil, "which", Which()):
            with agent_providers.catalog_snapshot():
                first = agent_providers.provider_catalog()
                first[0]["configured"] = "changed"
                first.pop()
                again = agent_providers.provider_catalog()
        self.assertEqual(len(again), 4)
        self.assertNotEqual(again[0]["configured"], "changed")

    def test_nothing_is_kept_after_the_block(self):
        which = Which()
        with clean_env(), mock.patch.object(agent_providers.shutil, "which", which):
            with agent_providers.catalog_snapshot():
                before = {item["id"]: item["configured"] for item in agent_providers.provider_catalog()}
            which.installed.add("claude")  # installed a moment later: the next load must see it
            after = {item["id"]: item["configured"] for item in agent_providers.provider_catalog()}
        self.assertFalse(before["claude-code"])
        self.assertTrue(after["claude-code"])

    def test_a_snapshot_inside_a_snapshot_shares_the_outer_one(self):
        which = Which()
        with clean_env(), mock.patch.object(agent_providers.shutil, "which", which):
            with agent_providers.catalog_snapshot():
                agent_providers.provider_catalog()
                with agent_providers.catalog_snapshot():
                    agent_providers.provider_catalog()
                agent_providers.provider_catalog()
        self.assertEqual(which.calls, 2)

    def test_an_error_inside_the_block_still_ends_the_snapshot(self):
        which = Which()
        with clean_env(), mock.patch.object(agent_providers.shutil, "which", which):
            with self.assertRaises(RuntimeError), agent_providers.catalog_snapshot():
                agent_providers.provider_catalog()
                raise RuntimeError("boom")
            which.installed.add("codex")
            self.assertTrue({item["id"]: item["configured"] for item in agent_providers.provider_catalog()}["codex-cli"])

    def test_a_patched_catalog_still_wins_inside_a_snapshot(self):
        fake = [{"id": "openai", "display_name": "OpenAI", "model": "m", "configured": True, "setup_hint": ""}]
        with clean_env(), mock.patch.object(agent_providers.shutil, "which", Which()), \
                mock.patch.object(agent_providers, "provider_catalog", return_value=fake):
            with agent_providers.catalog_snapshot():
                self.assertEqual(agent_providers.default_provider(), "openai")

    def test_the_default_provider_is_what_it_was_for_every_mix_of_keys_and_clis(self):
        for openai, anthropic, claude, codex in itertools.product((False, True), repeat=4):
            extra = {**({"OPENAI_API_KEY": "k"} if openai else {}), **({"ANTHROPIC_API_KEY": "k"} if anthropic else {})}
            installed = [name for name, present in (("claude", claude), ("codex", codex)) if present]
            with self.subTest(openai=openai, anthropic=anthropic, claude=claude, codex=codex):
                which = Which(*installed)
                with clean_env(**extra), mock.patch.object(agent_providers.shutil, "which", which):
                    now = agent_providers.default_provider()
                    one_build = which.calls
                    which.calls = 0
                    before = old_default_provider()
                    old_builds = which.calls
                self.assertEqual(now, before)
                self.assertEqual(one_build, 2, "one catalog for all four candidates")
                self.assertGreaterEqual(old_builds, one_build)

    def test_the_default_provider_is_legacy_when_nothing_is_set_up(self):
        with clean_env(), mock.patch.object(agent_providers.shutil, "which", Which()):
            self.assertEqual(agent_providers.default_provider(), "legacy")

    def test_a_patched_catalog_without_a_provider_skips_it(self):
        only_codex = [{"id": "codex-cli", "display_name": "Codex", "model": "m", "configured": True, "setup_hint": ""}]
        with clean_env(), mock.patch.object(agent_providers, "provider_catalog", return_value=only_codex):
            self.assertEqual(agent_providers.default_provider(), "codex-cli")
            self.assertEqual(old_default_provider(), "codex-cli")


class SettingsViewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        _, self.platform_path = build_and_migrate(self.root)
        self.settings = OutreachSettings(
            env_path=self.root / ".env", attachment_dir=self.root / "attachment", resume_storage=self.root / "resumes",
        )

    def view(self, *installed, **env):
        which = Which(*installed)
        with clean_env(**env), mock.patch.object(agent_providers.shutil, "which", which), \
                closing(connect_product(self.platform_path)) as conn:
            return self.settings.view(conn, user_id=USER), which.calls

    def view_without_snapshot(self, *installed, **env):
        """The same load with the snapshot switched off, which is how it ran before."""
        which = Which(*installed)
        with clean_env(**env), mock.patch.object(agent_providers.shutil, "which", which), \
                mock.patch("opportunity_app.outreach.settings.catalog_snapshot", contextlib.nullcontext), \
                closing(connect_product(self.platform_path)) as conn:
            return self.settings.view(conn, user_id=USER), which.calls

    def test_the_settings_view_is_identical_and_scans_path_once(self):
        cases = [
            ((), {}),
            (("claude",), {}),
            (("claude", "codex"), {"PIPELINE_OUTREACH_PROVIDER": "claude-code"}),
            (("codex",), {"OPENAI_API_KEY": "k", "PIPELINE_OUTREACH_REVIEW_PROVIDER": "openai"}),
            ((), {"ANTHROPIC_API_KEY": "k", "PIPELINE_OUTREACH_FOLLOW_UP_PROVIDER": "anthropic"}),
            (("claude", "codex"), {"PIPELINE_OUTREACH_REVIEW_PROVIDER": "nonsense"}),
        ]
        for installed, env in cases:
            with self.subTest(installed=installed, env=env):
                now, now_scans = self.view(*installed, **env)
                before, before_scans = self.view_without_snapshot(*installed, **env)
                self.assertEqual(now, before)
                self.assertEqual(now_scans, 2)
                self.assertGreater(before_scans, now_scans)


if __name__ == "__main__":
    unittest.main()
