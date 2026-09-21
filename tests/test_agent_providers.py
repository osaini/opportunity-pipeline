import json
import unittest
from types import SimpleNamespace

from opportunity_app.agent_providers import (
    AnthropicProvider,
    CliAgentProvider,
    OpenAIProvider,
    ToolDefinition,
)


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
        from opportunity_app.agent_providers import ProviderReply, ToolCall

        prior = ProviderReply(text="", tool_calls=[ToolCall(id="call-1", name="lookup", arguments={})], state=[{"role": "user", "content": "hi"}])
        reply = provider.continue_with(
            prior,
            [(prior.tool_calls[0], {"result": "ok"})],
            instructions="t",
            tools=[],
            max_output_tokens=50,
        )
        self.assertEqual(reply.text, "done")
        self.assertIn("lookup -> {\"result\": \"ok\"}", calls[0][2])

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
        self.assertEqual(calls[0][:3], ["codex", "exec", "--skip-git-repo-check"])


if __name__ == "__main__":
    unittest.main()
