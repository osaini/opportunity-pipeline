import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from opportunity_app.integrations.agent_providers import (
    AnthropicProvider,
    CliAgentProvider,
    OpenAIProvider,
    ToolDefinition,
    codex_command,
)

try:
    import realdata_guard
except ImportError:  # imported as tests.<module>, with tests/ not on sys.path
    from tests import realdata_guard
# Guards this module's own run against opening data/*.db (AGENTS.md hard rule 1); see tests/realdata_guard.py.
realdata_guard.install()


class RecordingEndpoint:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


class ProviderAdapterTests(unittest.TestCase):
    def test_openai_uses_strict_nonparallel_tools_and_omits_tools_for_plain_text(self):
        response = SimpleNamespace(
            id="response-1",
            output=[],
            output_text="done",
            usage=SimpleNamespace(input_tokens=3, output_tokens=2),
        )
        endpoint = RecordingEndpoint(response)
        provider = OpenAIProvider.__new__(OpenAIProvider)
        provider.model = "gpt-test"
        provider._client = SimpleNamespace(responses=endpoint)
        tool = ToolDefinition(
            "lookup",
            "Look up a record.",
            {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        )

        reply = provider.create(
            instructions="test",
            messages=[{"role": "user", "content": "hello"}],
            tools=[tool],
            max_output_tokens=100,
        )
        request = endpoint.calls[-1]
        self.assertFalse(request["parallel_tool_calls"])
        self.assertTrue(request["tools"][0]["strict"])
        self.assertFalse(request["store"])
        self.assertEqual(reply.input_tokens, 3)

        provider.create(
            instructions="test",
            messages=[{"role": "user", "content": "write"}],
            tools=[],
            max_output_tokens=100,
        )
        self.assertNotIn("tools", endpoint.calls[-1])
        self.assertNotIn("parallel_tool_calls", endpoint.calls[-1])

    def test_anthropic_disables_parallel_tools_and_omits_tools_for_plain_text(self):
        response = SimpleNamespace(
            id="message-1",
            content=[],
            usage=SimpleNamespace(input_tokens=5, output_tokens=1),
        )
        endpoint = RecordingEndpoint(response)
        provider = AnthropicProvider.__new__(AnthropicProvider)
        provider.model = "claude-test"
        provider._client = SimpleNamespace(messages=endpoint)
        tool = ToolDefinition(
            "lookup",
            "Look up a record.",
            {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        )

        provider.create(
            instructions="test",
            messages=[{"role": "user", "content": "hello"}],
            tools=[tool],
            max_output_tokens=100,
        )
        request = endpoint.calls[-1]
        self.assertTrue(request["tool_choice"]["disable_parallel_tool_use"])
        self.assertTrue(request["tools"][0]["strict"])

        provider.create(
            instructions="test",
            messages=[{"role": "user", "content": "write"}],
            tools=[],
            max_output_tokens=100,
        )
        self.assertNotIn("tools", endpoint.calls[-1])
        self.assertNotIn("tool_choice", endpoint.calls[-1])


class CliAgentProviderTests(unittest.TestCase):
    def _provider(self, stdout, returncode=0):
        calls = []

        def runner(command):
            calls.append(command)
            return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")

        provider = CliAgentProvider("claude-code", "subscription", runner=runner)
        return provider, calls

    def test_tool_decision_is_parsed_into_a_single_call(self):
        payload = json.dumps({"tool": "search_opportunities", "arguments": {"query": "mechanical"}})
        provider, calls = self._provider(f'some cli noise\n{payload}\n')
        reply = provider.create(
            instructions="test",
            messages=[{"role": "user", "content": "find roles"}],
            tools=[ToolDefinition("search_opportunities", "Search.", {"type": "object"})],
            max_output_tokens=100,
        )
        self.assertEqual(len(reply.tool_calls), 1)
        self.assertEqual(reply.tool_calls[0].name, "search_opportunities")
        self.assertEqual(reply.tool_calls[0].arguments["query"], "mechanical")
        # The CLI is invoked headless with the full prompt.
        self.assertIn("--output-format", calls[0])

    def test_plain_answer_and_fenced_json_are_tolerated(self):
        provider, _ = self._provider('```json\n{"answer": "Apply to Acme first."}\n```')
        reply = provider.create(instructions="t", messages=[{"role": "user", "content": "hi"}], tools=[], max_output_tokens=50)
        self.assertEqual(reply.text, "Apply to Acme first.")
        self.assertEqual(reply.tool_calls, [])

    def test_continue_with_appends_tool_results_to_transcript(self):
        payload = json.dumps({"answer": "done"})
        provider, calls = self._provider(payload)
        from opportunity_app.integrations.agent_providers import ProviderReply, ToolCall

        prior = ProviderReply(text="", tool_calls=[ToolCall(id="call-1", name="lookup", arguments={})], state=[{"role": "user", "content": "hi"}])
        reply = provider.continue_with(
            prior,
            [(prior.tool_calls[0], {"result": "ok"})],
            instructions="t",
            tools=[],
            max_output_tokens=50,
        )
        self.assertEqual(reply.text, "done")
        # The prompt travels over stdin: the runner receives [*command, stdin].
        self.assertIn("lookup -> {\"result\": \"ok\"}", calls[0][-1])

    def test_cli_failure_raises_runtime_error(self):
        provider, _ = self._provider("", returncode=1)
        with self.assertRaises(RuntimeError):
            provider.create(instructions="t", messages=[{"role": "user", "content": "hi"}], tools=[], max_output_tokens=10)

    def test_non_json_reply_is_rejected_honestly(self):
        provider, _ = self._provider("I cannot answer that in JSON.")
        with self.assertRaises(ValueError):
            provider.create(instructions="t", messages=[{"role": "user", "content": "hi"}], tools=[], max_output_tokens=10)

    def test_codex_command_shape(self):
        calls = []

        def runner(command):
            calls.append(command)
            return SimpleNamespace(returncode=0, stdout='{"answer": "ok"}', stderr="")

        provider = CliAgentProvider("codex-cli", "subscription", runner=runner)
        reply = provider.create(instructions="t", messages=[{"role": "user", "content": "hi"}], tools=[], max_output_tokens=10)
        self.assertEqual(reply.text, "ok")
        self.assertEqual(calls[0][:3], ["codex", "exec", "--sandbox"])
        self.assertIn("--ignore-user-config", calls[0])


# A stand-in for the CLI: reports the stdin it received, its working directory
# inside a JSON answer, writing UTF-8 bytes like the real CLIs.
_FAKE_CLI = r"""
import json, os, sys
data = sys.stdin.buffer.read().decode("utf-8")
answer = "stdin=%d cwd=%s argc=%d dash=— snow=☃ emoji=\U0001F600" % (len(data), os.getcwd(), len(sys.argv))
sys.stdout.buffer.write(json.dumps({"answer": answer}, ensure_ascii=False).encode("utf-8"))
"""

SCRAPED = {"description": "SCRAPED “quote” & whoami | calc"}


class CliAgentProviderSubprocessTests(unittest.TestCase):
    """The chat path must be as sandboxed as complete_text: no scraped text on
    argv, no project cwd, no default tools, UTF-8 decoding."""

    def assert_throwaway_cwd(self, cwd):
        """A fresh directory under the system temp, gone once the CLI has finished."""
        self.assertEqual(Path(cwd).parent, Path(tempfile.gettempdir()))
        self.assertNotEqual(Path(cwd), Path(tempfile.gettempdir()))
        self.assertFalse(Path(cwd).exists(), "the working directory is removed afterwards")

    def _run_chat(self, provider_id):
        captured = []
        real_run = subprocess.run

        def fake_run(command, **kwargs):
            captured.append((list(command), kwargs))
            # Run the stand-in CLI with the exact kwargs the provider chose.
            return real_run([sys.executable, "-c", _FAKE_CLI], **kwargs)

        from opportunity_app.integrations.agent_providers import ProviderReply, ToolCall

        provider = CliAgentProvider(provider_id, "subscription")
        provider.binary = provider_id
        prior = ProviderReply(text="", tool_calls=[ToolCall(id="c1", name="lookup", arguments={})], state=[{"role": "user", "content": "hi"}])
        with mock.patch("opportunity_app.integrations.agent_providers.subprocess.run", fake_run):
            reply = provider.continue_with(
                prior, [(prior.tool_calls[0], SCRAPED)], instructions="Be careful.", tools=[], max_output_tokens=50,
            )
        return reply, captured

    def test_claude_chat_sends_prompt_on_stdin_without_tools_in_a_temp_cwd(self):
        _, captured = self._run_chat("claude-code")
        command, kwargs = captured[0]
        self.assertEqual(command, ["claude-code", "-p", "--output-format", "text", "--tools", "", "--strict-mcp-config"])
        self.assertIn("SCRAPED", kwargs["input"])
        self.assertIn("Be careful.", kwargs["input"])
        self.assertFalse(any("SCRAPED" in part or "Be careful." in part for part in command), "the prompt is not on argv")
        self.assert_throwaway_cwd(kwargs["cwd"])
        self.assertEqual(kwargs["encoding"], "utf-8")
        self.assertEqual(kwargs["errors"], "replace")

    def test_codex_chat_sends_prompt_on_stdin_with_the_isolated_command(self):
        _, captured = self._run_chat("codex-cli")
        command, kwargs = captured[0]
        self.assertEqual(command, codex_command("codex-cli"))
        self.assertIn("SCRAPED", kwargs["input"])
        self.assertFalse(any("SCRAPED" in part for part in command), "the prompt is not on argv")
        self.assert_throwaway_cwd(kwargs["cwd"])
        self.assertEqual(kwargs["encoding"], "utf-8")

    def test_chat_reply_with_non_ascii_text_is_decoded_as_utf8(self):
        reply, _ = self._run_chat("claude-code")
        self.assertIn("dash=— snow=☃ emoji=\U0001F600", reply.text)


if __name__ == "__main__":
    unittest.main()
