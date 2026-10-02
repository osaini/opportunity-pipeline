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


_HOME = None


def setUpModule():
    """The tests never read the developer's real ~/.codex/config.toml: CODEX_HOME is an empty directory for the module."""
    global _HOME
    _HOME = tempfile.TemporaryDirectory()
    patcher = mock.patch.dict("os.environ", {"CODEX_HOME": _HOME.name, "PIPELINE_CODEX_MODEL": "", "PIPELINE_CODEX_REASONING_EFFORT": ""})
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)
    unittest.addModuleCleanup(_HOME.cleanup)


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
            with self.subTest(web=web), mock.patch.dict("os.environ", {ALLOW_CODEX_ENV: "1"}),                     mock.patch.object(agent_providers.subprocess, "run", return_value=done) as run:
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
        with mock.patch.dict("os.environ", {ALLOW_CODEX_ENV: "1"}):
            left_on = ["web_search=disabled" if item == "web_search=live" else item for item in self.good(web=True)]
            self.assert_refused(left_on, "code_mode_host")

    def test_web_search_live_is_refused_unless_the_students_opt_in_is_set(self):
        """The research runner is the only builder of web_search=live, and only after the opt-in; any other argv with it
        is a command somebody wrote by hand."""
        with mock.patch.dict("os.environ", {ALLOW_CODEX_ENV: "1"}):
            live = self.good(web=True)
        for value in ("", "0", "no"):
            with self.subTest(opt_in=value), mock.patch.dict("os.environ", {ALLOW_CODEX_ENV: value}):
                self.assert_refused(live, "web_search=live")
        with mock.patch.dict("os.environ", {ALLOW_CODEX_ENV: "1"}):
            agent_providers.require_codex_isolation(live)

    def test_the_student_agent_refuses_a_command_that_lost_the_isolation(self):
        """The provider checks even when a runner is injected, so a test double cannot hide a weakened command."""
        provider = codex_provider(runner=lambda command: SimpleNamespace(returncode=0, stdout="{}", stderr=""))
        with self.assertRaises(agent_providers.CodexNotIsolated):
            provider._invoke(["codex", "exec", "--skip-git-repo-check", "--sandbox", "read-only", "-"], "prompt")

    def test_other_commands_are_not_checked(self):
        done = subprocess.CompletedProcess([], 0, "ok", "")
        with mock.patch.object(agent_providers.subprocess, "run", return_value=done):
            agent_providers.run_headless(["claude", *agent_providers.CLAUDE_NO_TOOLS], "p", timeout=5, cwd=".")

    def test_an_option_that_undoes_the_isolation_is_refused_in_every_spelling(self):
        """Presence checks alone would accept a command that adds settings back; only options the builder writes pass."""
        command = self.good()
        for extra in (
            ["-c", "mcp_servers.x.command=evil"], ["-c", "sandbox_mode=danger-full-access"], ["--config", "web_search=live"],
            ["--config=web_search=live"], ["-c", "features.shell_tool=true"], ["-cweb_search=live"],
            ["--sandbox=danger-full-access"], ["--profile=mine"], ["--add-dir=/"], ["-m", "--oss"], ["-m", ""],
            ["--model", "x"], ["--full-auto"], ["-c", "model_reasoning_effort=ludicrous"], ["--disable=code_mode_host"],
            ["--enable=shell_tool"], ["--enable", "code_mode_host"], ["--sandbox=read-only"], ["-c", "web_search=live"],
            ["-c", "model_reasoning_effort=high", "-c", "approval_policy=never"], ["--config", "model_reasoning_effort=low"],
        ):
            with self.subTest(extra=extra):
                self.assert_refused([*command[:-1], *extra, "-"])

    def test_a_model_and_effort_that_the_builder_writes_are_accepted(self):
        with mock.patch.object(agent_providers, "codex_model_settings", return_value=("gpt-6.1-sol", "low")):
            command = agent_providers.codex_command("codex")
        self.assertEqual(values_after(command, "-m"), ["gpt-6.1-sol"])
        self.assertIn("model_reasoning_effort=low", values_after(command, "-c"))
        agent_providers.require_codex_isolation(command)

    def test_a_cli_too_old_for_the_flags_says_to_update_it(self):
        for message in ("error: unexpected argument '--ignore-user-config' found", "Error: Unknown feature flag: tool_suggest",
                        "Error loading config.toml: unknown configuration field `web_search`"):
            with self.subTest(message=message):
                detail = agent_providers.codex_failure_detail(subprocess.CompletedProcess([], 2, "", message))
                self.assertIn(message, detail)
                self.assertIn("update", detail.lower())
        self.assertEqual(agent_providers.codex_failure_detail(subprocess.CompletedProcess([], 1, "", "not signed in")), "not signed in")


class TheStudentsModelSurvivesTheIsolationTests(unittest.TestCase):
    """--ignore-user-config stops Codex reading config.toml, so the model and effort the student chose go on argv."""

    def home(self, text):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        if text is not None:
            (Path(folder.name) / "config.toml").write_text(text, encoding="utf-8")
        return mock.patch.dict("os.environ", {"CODEX_HOME": folder.name, "PIPELINE_CODEX_MODEL": "", "PIPELINE_CODEX_REASONING_EFFORT": ""})

    def test_the_model_and_effort_come_from_config_toml(self):
        with self.home('model = "gpt-6.1-sol"\nmodel_reasoning_effort = "low"\nsandbox_mode = "danger-full-access"\n'):
            command = agent_providers.codex_command("codex")
            self.assertEqual(agent_providers.codex_model_settings(), ("gpt-6.1-sol", "low"))
        self.assertEqual(values_after(command, "-m"), ["gpt-6.1-sol"])
        self.assertIn("model_reasoning_effort=low", values_after(command, "-c"))
        self.assertNotIn("danger-full-access", command, "no other key of that file reaches the command")
        assert_isolated(self, command, web=False)

    def test_the_env_settings_win_over_the_file(self):
        with self.home('model = "from-file"\nmodel_reasoning_effort = "low"\n'), \
                mock.patch.dict("os.environ", {"PIPELINE_CODEX_MODEL": "mine", "PIPELINE_CODEX_REASONING_EFFORT": "HIGH"}):
            self.assertEqual(agent_providers.codex_model_settings(), ("mine", "high"))

    def test_nothing_is_passed_when_nothing_is_set_or_the_file_is_missing_or_broken(self):
        for text in (None, "", "model = [", 'model = 3\nmodel_reasoning_effort = ["low"]\n'):
            with self.subTest(text=text), self.home(text):
                self.assertEqual(agent_providers.codex_model_settings(), ("", ""))
                command = agent_providers.codex_command("codex")
                self.assertNotIn("-m", command)
                self.assertEqual([item for item in values_after(command, "-c") if item.startswith("model_reasoning")], [])

    def test_a_value_that_is_not_a_model_name_or_an_effort_cannot_put_an_option_on_argv(self):
        with self.home('model = "--oss"\nmodel_reasoning_effort = "sandbox_mode=danger-full-access"\n'):
            self.assertEqual(agent_providers.codex_model_settings(), ("", ""))


class CodexWithoutTheOptInFallsBackToClaudeCodeTests(unittest.TestCase):
    """The deep search, contact searches and the CLI do not just fail when Codex is chosen: Claude Code runs and says so."""

    def agents(self, *, claude_installed, allowed):
        env = {ALLOW_CODEX_ENV: "1" if allowed else ""}
        return mock.patch.dict("os.environ", env), mock.patch.object(
            outreach_agents, "cli_available", lambda binary: claude_installed or "claude" not in str(binary).lower())

    def test_codex_chosen_without_the_opt_in_runs_claude_code_with_a_note(self):
        env, installed = self.agents(claude_installed=True, allowed=False)
        with env, installed:
            provider, note = outreach_agents.resolve_discovery_agent("codex-cli")
            runner, runner_note = outreach_agents.agent_runner("codex-cli")
        self.assertEqual(provider, "claude-code")
        self.assertIn("Claude Code ran this search", note)
        self.assertIn(ALLOW_CODEX_ENV, note)
        self.assertIs(runner, outreach_agents.RUNNERS["claude-code"])
        self.assertEqual(runner_note, note)

    def test_no_fallback_with_the_opt_in_with_claude_code_chosen_or_with_only_codex_installed(self):
        for chosen, claude_installed, allowed in (("codex-cli", True, True), ("claude-code", True, False), ("codex-cli", False, False)):
            env, installed = self.agents(claude_installed=claude_installed, allowed=allowed)
            with self.subTest(chosen=chosen, claude_installed=claude_installed, allowed=allowed), env, installed:
                self.assertEqual(outreach_agents.resolve_discovery_agent(chosen), (chosen, ""))

    def test_a_blank_provider_is_left_alone_so_the_documented_cli_defect_stays_as_it_was(self):
        env, installed = self.agents(claude_installed=True, allowed=False)
        with env, installed:
            self.assertEqual(outreach_agents.resolve_discovery_agent(""), ("", ""))

    def test_the_cli_says_so_on_stderr_and_uses_claude_code(self):
        import io
        from contextlib import redirect_stderr
        from opportunity_app import outreach_cli

        used = []
        env, installed = self.agents(claude_installed=True, allowed=False)
        fakes = {"claude-code": lambda prompt: used.append("claude") or "{}", "codex-cli": lambda prompt: used.append("codex") or "{}"}
        with env, installed, mock.patch.dict(outreach_cli.RUNNERS, fakes):
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                runner = outreach_cli._runner_for("codex-cli")
            runner("p")
        self.assertEqual(used, ["claude"])
        self.assertIn("Claude Code ran this search", stderr.getvalue())

    def test_settings_do_not_offer_codex_for_research_until_the_opt_in_is_set(self):
        from opportunity_app.outreach.settings import OutreachSettings

        option = {"id": "codex-cli", "label": "Codex", "available": True, "hint": ""}
        with mock.patch.dict("os.environ", {ALLOW_CODEX_ENV: ""}):
            shown = OutreachSettings._research_option(option)
        self.assertFalse(shown["available"])
        self.assertIn(ALLOW_CODEX_ENV, shown["hint"])
        self.assertIn("Claude Code", shown["hint"])
        with mock.patch.dict("os.environ", {ALLOW_CODEX_ENV: "1"}):
            self.assertEqual(OutreachSettings._research_option(option), option)
        other = {"id": "claude-code", "label": "Claude", "available": True, "hint": ""}
        self.assertEqual(OutreachSettings._research_option(other), other)


if __name__ == "__main__":
    unittest.main()
