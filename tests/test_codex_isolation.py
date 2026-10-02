"""Every Codex CLI call the app makes runs with no MCP servers, shell, web search or file writes, in a throwaway directory.

The Claude path gets this from ``--tools "" --strict-mcp-config``. Codex has no single switch for it, so the app builds
every Codex command through ``agent_providers.codex_command`` and refuses to start one that lacks the isolation. These
tests state the contract independently of the builder (the expected flags are written out here, not imported), run each
real command builder, and show that a command missing any piece is refused before a process starts.
"""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from opportunity_app.integrations import agent_providers
from opportunity_app.outreach import agents as outreach_agents, review as outreach_review
from opportunity_app.outreach.research import ALLOW_CODEX_ENV

from helpers_source import python_modules

try:
    import realdata_guard
except ImportError:  # imported as tests.<module>, with tests/ not on sys.path
    from tests import realdata_guard
# Guards this module's own run against opening data/*.db (AGENTS.md hard rule 1); see tests/realdata_guard.py.
realdata_guard.install()

# Written out here on purpose: a change to the builder's list must show up as a change to this contract too.
ALWAYS_OFF = {
    "shell_tool", "unified_exec", "plugins", "apps", "multi_agent", "browser_use", "computer_use", "in_app_browser",
    "view_image", "image_generation", "goals", "memories", "hooks", "skill_search", "tool_suggest", "sleep_tool",
}
SWITCHES = ("--skip-git-repo-check", "--ignore-user-config", "--ignore-rules", "--ephemeral", "--strict-config")
NEVER = ("--enable", "--add-dir", "--dangerously-bypass-approvals-and-sandbox", "--dangerously-bypass-hook-trust",
         "--approve-for-me", "--oss", "--profile", "-p", "danger-full-access", "workspace-write")


def values_after(args, flag):
    return [args[index + 1] for index, item in enumerate(args[:-1]) if item == flag]


def assert_isolated(test, command, *, web):
    """The contract for one Codex argv. ``web`` is True only for the company-research runner."""
    args = list(command[1:])
    test.assertEqual(args[0], "exec", command)
    for switch in SWITCHES:
        test.assertIn(switch, args, f"{switch} missing from {command}")
    test.assertEqual(values_after(args, "--sandbox"), ["read-only"], "the read-only sandbox stays as the second layer")
    overrides = values_after(args, "-c")
    test.assertIn("mcp_servers={}", overrides)
    test.assertIn("shell_environment_policy.inherit=none", overrides)
    test.assertEqual([item for item in overrides if item.startswith("web_search")], ["web_search=live" if web else "web_search=disabled"])
    off = set(values_after(args, "--disable"))
    test.assertTrue(ALWAYS_OFF <= off, f"not turned off: {sorted(ALWAYS_OFF - off)}")
    if web:
        test.assertNotIn("code_mode_host", off, "web search is reached through code mode")
    else:
        test.assertIn("code_mode_host", off, "code mode reaches apply_patch and the web tool, so it is off unless web search is wanted")
    for forbidden in NEVER:
        test.assertNotIn(forbidden, args)
    test.assertEqual(args[-1], "-", "the prompt goes over stdin, never on argv")


def codex_provider(**kwargs):
    provider = agent_providers.CliAgentProvider("codex-cli", "subscription", **kwargs)
    provider.binary = "codex"
    return provider


class EveryCodexBuilderIsIsolatedTests(unittest.TestCase):
    def test_the_student_agent_command(self):
        assert_isolated(self, codex_provider()._command(), web=False)

    def test_the_student_agent_chat_runs_the_isolated_command_in_a_throwaway_directory(self):
        seen = []

        def fake_run(command, **kwargs):
            listing = sorted(os.listdir(kwargs["cwd"]))
            seen.append((list(command), kwargs["cwd"], listing))
            return subprocess.CompletedProcess(command, 0, '{"answer": "ok"}', "")

        with mock.patch.object(agent_providers.subprocess, "run", fake_run):
            reply = codex_provider().create(instructions="t", messages=[{"role": "user", "content": "hi"}], tools=[], max_output_tokens=10)
        self.assertEqual(reply.text, "ok")
        command, cwd, listing = seen[0]
        assert_isolated(self, command, web=False)
        self.assertEqual(listing, [], "the directory is empty")
        self.assertEqual(Path(cwd).parent, Path(tempfile.gettempdir()))

    def test_the_complete_text_command(self):
        seen = []

        def runner(command):
            seen.append(command)
            return SimpleNamespace(returncode=0, stdout="done", stderr="")

        self.assertEqual(codex_provider(runner=runner).complete_text("Say it.", "content"), "done")
        assert_isolated(self, seen[0][:-1], web=False)

    def test_the_outreach_reviewer_command(self):
        seen = []

        def fake(command, prompt, *, timeout, cwd):
            seen.append((list(command), cwd, sorted(os.listdir(cwd))))
            answer = values_after(command, "--output-last-message")[0]
            Path(answer).write_text('{"send": true, "problems": []}', encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, "", "")

        catalog = [{"id": "codex-cli", "display_name": "Codex", "model": "m", "configured": True, "setup_hint": ""}]
        with mock.patch.dict("os.environ", {"PIPELINE_OUTREACH_REVIEW_PROVIDER": "codex-cli"}), \
                mock.patch.object(agent_providers, "provider_catalog", return_value=catalog), \
                mock.patch.object(outreach_review, "run_headless", fake):
            _, run = outreach_review.review_runner()
            self.assertIn('"send": true', run("review this"))
        command, cwd, listing = seen[0]
        assert_isolated(self, command, web=False)
        self.assertIn("--output-last-message", command)
        self.assertEqual(listing, [], "the directory is empty when the reviewer starts")
        self.assertEqual(Path(cwd).parent, Path(tempfile.gettempdir()))

    def test_the_web_research_runner_command_is_the_only_one_with_web_search(self):
        seen = []

        def fake(command, prompt, *, timeout, cwd):
            seen.append((list(command), cwd, sorted(os.listdir(cwd))))
            return subprocess.CompletedProcess(command, 0, "found it", "")

        with mock.patch.dict("os.environ", {ALLOW_CODEX_ENV: "1"}), mock.patch.object(outreach_agents, "run_headless", fake):
            self.assertEqual(outreach_agents.codex_runner("research this"), "found it")
        command, cwd, listing = seen[0]
        assert_isolated(self, command, web=True)
        self.assertEqual(listing, [])
        self.assertEqual(Path(cwd).parent, Path(tempfile.gettempdir()))

    def test_web_research_on_codex_is_refused_until_the_student_accepts_it(self):
        """Code mode is what reaches the web tool, and it also exposes apply_patch (which can test whether a local file
        holds given lines), so Codex does not read pages nobody vetted unless .env says the student accepts that."""
        started = []
        with mock.patch.dict("os.environ", {ALLOW_CODEX_ENV: ""}), \
                mock.patch.object(outreach_agents, "run_headless", lambda *args, **kwargs: started.append(args)):
            with self.assertRaises(agent_providers.CodexNotIsolated) as raised:
                outreach_agents.codex_runner("research this")
        self.assertEqual(started, [], "nothing was started")
        self.assertIn(ALLOW_CODEX_ENV, str(raised.exception))
        self.assertIn("Claude Code", str(raised.exception))

    def test_no_module_builds_a_codex_command_by_hand(self):
        """Source guard over the whole package: a second builder would skip the isolation (and this file)."""
        for name, text in python_modules("*.py", exclude=("integrations/agent_providers.py",)).items():
            with self.subTest(module=name):
                self.assertFalse("CODEX_READ_ONLY" in text, "the old sandbox-only flags are gone")
                self.assertFalse('"--sandbox"' in text or "'--sandbox'" in text, "a Codex argv is not written out by hand")
                if 'cli_binary("codex-cli")' in text:
                    self.assertTrue("codex_command(" in text, "a module that names the Codex binary builds the command with codex_command")


class AnUnisolatedCodexCommandIsRefusedTests(unittest.TestCase):
    def good(self, *, web=False):
        return agent_providers.codex_command("codex", web_search=web)

    def assert_refused(self, command, fragment=""):
        with mock.patch.object(agent_providers.subprocess, "run") as run:
            with self.assertRaises(agent_providers.CodexNotIsolated) as raised:
                agent_providers.run_headless(command, "prompt", timeout=5, cwd=".")
        run.assert_not_called()
        self.assertIn(fragment, str(raised.exception))

    def test_the_builders_output_is_accepted(self):
        for web in (False, True):
            done = subprocess.CompletedProcess([], 0, "ok", "")
            with self.subTest(web=web), mock.patch.object(agent_providers.subprocess, "run", return_value=done) as run:
                agent_providers.run_headless(self.good(web=web), "prompt", timeout=5, cwd=".")
            run.assert_called_once()

    def test_the_old_sandbox_only_command_is_refused(self):
        self.assert_refused(["codex", "exec", "--skip-git-repo-check", "--sandbox", "read-only", "-"], "--ignore-user-config")

    def test_a_command_missing_any_one_switch_override_or_feature_is_refused(self):
        command = self.good()
        for index, item in enumerate(command):
            if item in ("--ignore-user-config", "--ignore-rules", "--ephemeral", "--strict-config", "--skip-git-repo-check"):
                with self.subTest(dropped=item):
                    self.assert_refused(command[:index] + command[index + 1:], item)
            if item in ("-c", "--disable", "--sandbox"):
                with self.subTest(dropped=f"{item} {command[index + 1]}"):
                    self.assert_refused(command[:index] + command[index + 2:])

    def test_a_looser_sandbox_or_an_enabled_feature_is_refused(self):
        command = self.good()
        loosened = list(command)
        loosened[loosened.index("--sandbox") + 1] = "workspace-write"
        self.assert_refused(loosened, "read-only")
        for extra in (["--enable", "shell_tool"], ["--add-dir", "."], ["--dangerously-bypass-approvals-and-sandbox"], ["-p", "mine"]):
            with self.subTest(extra=extra):
                self.assert_refused([*command[:-1], *extra, "-"])

    def test_web_search_must_be_named_and_code_mode_stays_off_without_it(self):
        command = self.good()
        self.assert_refused([item for item in command if item != "web_search=disabled"], "web_search")
        for value in ("cached", "true", ""):
            with self.subTest(value=value):
                self.assert_refused(["web_search=" + value if item == "web_search=disabled" else item for item in command], "web_search")
        # The web-research command has code mode on; the same command with web search off is a command that left it on.
        left_on = ["web_search=disabled" if item == "web_search=live" else item for item in self.good(web=True)]
        self.assert_refused(left_on, "code_mode_host")

    def test_the_student_agent_refuses_a_command_that_lost_the_isolation(self):
        """The provider checks even when a runner is injected, so a test double cannot hide a weakened command."""
        provider = codex_provider(runner=lambda command: SimpleNamespace(returncode=0, stdout="{}", stderr=""))
        with self.assertRaises(agent_providers.CodexNotIsolated):
            provider._invoke(["codex", "exec", "--skip-git-repo-check", "--sandbox", "read-only", "-"], "prompt")

    def test_other_commands_are_not_checked(self):
        done = subprocess.CompletedProcess([], 0, "ok", "")
        with mock.patch.object(agent_providers.subprocess, "run", return_value=done):
            agent_providers.run_headless(["claude", *agent_providers.CLAUDE_NO_TOOLS], "p", timeout=5, cwd=".")

    def test_a_cli_too_old_for_the_flags_says_to_update_it(self):
        for message in ("error: unexpected argument '--ignore-user-config' found", "Error: Unknown feature flag: tool_suggest",
                        "Error loading config.toml: unknown configuration field `web_search`"):
            with self.subTest(message=message):
                detail = agent_providers.codex_failure_detail(subprocess.CompletedProcess([], 2, "", message))
                self.assertIn(message, detail)
                self.assertIn("update", detail.lower())
        self.assertEqual(agent_providers.codex_failure_detail(subprocess.CompletedProcess([], 1, "", "not signed in")), "not signed in")


if __name__ == "__main__":
    unittest.main()
