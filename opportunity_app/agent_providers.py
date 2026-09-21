"""Provider-neutral model and tool-call adapters for the student agent."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class ProviderReply:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    request_id: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    state: Any = None


class AgentProvider(Protocol):
    name: str
    model: str

    def create(
        self,
        *,
        instructions: str,
        messages: list[dict[str, str]],
        tools: list[ToolDefinition],
        max_output_tokens: int,
    ) -> ProviderReply: ...

    def continue_with(
        self,
        reply: ProviderReply,
        results: list[tuple[ToolCall, dict[str, Any]]],
        *,
        instructions: str,
        tools: list[ToolDefinition],
        max_output_tokens: int,
    ) -> ProviderReply: ...


def provider_catalog() -> list[dict[str, Any]]:
    """Return safe provider metadata; secrets never leave the server."""

    values = [
        (
            "openai",
            "OpenAI",
            os.environ.get("OPENAI_AGENT_MODEL", "gpt-5.4"),
            bool(os.environ.get("OPENAI_API_KEY")),
            "Set OPENAI_API_KEY to enable OpenAI.",
        ),
        (
            "anthropic",
            "Anthropic",
            os.environ.get("ANTHROPIC_AGENT_MODEL", "claude-sonnet-5"),
            bool(os.environ.get("ANTHROPIC_API_KEY")),
            "Set ANTHROPIC_API_KEY to enable Anthropic.",
        ),
        (
            "claude-code",
            "Claude Code (subscription)",
            os.environ.get("CLAUDE_AGENT_MODEL", "subscription"),
            _cli_available(_cli_binary("claude-code")),
            "Install Claude Code and log in (`claude`) to use your Claude subscription.",
        ),
        (
            "codex-cli",
            "Codex CLI (subscription)",
            os.environ.get("CODEX_AGENT_MODEL", "subscription"),
            _cli_available(_cli_binary("codex-cli")),
            "Install Codex CLI and log in (`codex login`) to use your ChatGPT subscription.",
        ),
    ]
    return [
        {
            "id": identifier,
            "display_name": display_name,
            "model": model,
            "configured": configured,
            "setup_hint": "" if configured else setup_hint,
        }
        for identifier, display_name, model, configured, setup_hint in values
    ]


CLI_CONFIG = {
    # id -> (binary name on PATH, env override for the binary path)
    "claude-code": ("claude", "PIPELINE_CLAUDE_BIN"),
    "codex-cli": ("codex", "PIPELINE_CODEX_BIN"),
}


def _cli_binary(provider_id: str) -> str:
    binary, override_env = CLI_CONFIG[provider_id]
    return os.environ.get(override_env) or binary


def _cli_available(binary: str) -> bool:
    return bool(shutil.which(binary))


def configured_provider(provider: str) -> dict[str, Any]:
    record = next((item for item in provider_catalog() if item["id"] == provider), None)
    if record is None:
        raise ValueError("Provider must be openai, anthropic, claude-code, codex-cli, or legacy")
    if not record["configured"]:
        raise ValueError(record["setup_hint"])
    return record


def default_provider() -> str:
    """Prefer a real model provider when credentials exist; legacy keyword
    mode stays as the honest degraded fallback."""
    for candidate in ("openai", "anthropic", "claude-code", "codex-cli"):
        try:
            configured_provider(candidate)
            return candidate
        except ValueError:
            continue
    return "legacy"


class OpenAIProvider:
    name = "openai"

    def __init__(self, model: str, *, timeout: float = 45.0):
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover - deployment dependency
            raise RuntimeError("OpenAI support requires the `openai` package") from exc
        self.model = model
        self._client = OpenAI(timeout=timeout)

    @staticmethod
    def _tools(tools: list[ToolDefinition]) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
                "strict": True,
            }
            for tool in tools
        ]

    @staticmethod
    def _reply(response: Any, input_items: list[Any]) -> ProviderReply:
        calls: list[ToolCall] = []
        for item in response.output:
            if getattr(item, "type", "") != "function_call":
                continue
            import json

            calls.append(
                ToolCall(
                    id=str(item.call_id),
                    name=str(item.name),
                    arguments=json.loads(item.arguments or "{}"),
                )
            )
        serialized = [
            item.model_dump(exclude_none=True) if hasattr(item, "model_dump") else item
            for item in response.output
        ]
        usage = getattr(response, "usage", None)
        return ProviderReply(
            text=str(getattr(response, "output_text", "") or ""),
            tool_calls=calls,
            request_id=str(getattr(response, "id", "") or ""),
            input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
            state=[*input_items, *serialized],
        )

    def create(self, *, instructions: str, messages: list[dict[str, str]], tools: list[ToolDefinition], max_output_tokens: int) -> ProviderReply:
        input_items: list[Any] = [dict(message) for message in messages]
        request: dict[str, Any] = {
            "model": self.model,
            "instructions": instructions,
            "input": input_items,
            "max_output_tokens": max_output_tokens,
            "store": False,
        }
        if tools:
            request.update(tools=self._tools(tools), parallel_tool_calls=False)
        response = self._client.responses.create(**request)
        return self._reply(response, input_items)

    def continue_with(self, reply: ProviderReply, results: list[tuple[ToolCall, dict[str, Any]]], *, instructions: str, tools: list[ToolDefinition], max_output_tokens: int) -> ProviderReply:
        import json

        input_items = list(reply.state or [])
        input_items.extend(
            {
                "type": "function_call_output",
                "call_id": call.id,
                "output": json.dumps(result),
            }
            for call, result in results
        )
        request: dict[str, Any] = {
            "model": self.model,
            "instructions": instructions,
            "input": input_items,
            "max_output_tokens": max_output_tokens,
            "store": False,
        }
        if tools:
            request.update(tools=self._tools(tools), parallel_tool_calls=False)
        response = self._client.responses.create(**request)
        return self._reply(response, input_items)


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, model: str, *, timeout: float = 45.0):
        try:
            from anthropic import Anthropic
        except ImportError as exc:  # pragma: no cover - deployment dependency
            raise RuntimeError("Anthropic support requires the `anthropic` package") from exc
        self.model = model
        self._client = Anthropic(timeout=timeout)

    @staticmethod
    def _tools(tools: list[ToolDefinition]) -> list[dict[str, Any]]:
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": tool.parameters,
                "strict": True,
            }
            for tool in tools
        ]

    @staticmethod
    def _content(content: Any) -> list[dict[str, Any]]:
        return [
            block.model_dump(exclude_none=True) if hasattr(block, "model_dump") else dict(block)
            for block in content
        ]

    @classmethod
    def _reply(cls, response: Any, messages: list[dict[str, Any]]) -> ProviderReply:
        calls = [
            ToolCall(id=str(block.id), name=str(block.name), arguments=dict(block.input or {}))
            for block in response.content
            if getattr(block, "type", "") == "tool_use"
        ]
        text = "\n".join(
            str(block.text)
            for block in response.content
            if getattr(block, "type", "") == "text" and getattr(block, "text", "")
        )
        usage = getattr(response, "usage", None)
        return ProviderReply(
            text=text,
            tool_calls=calls,
            request_id=str(getattr(response, "id", "") or ""),
            input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
            state=[*messages, {"role": "assistant", "content": cls._content(response.content)}],
        )

    def create(self, *, instructions: str, messages: list[dict[str, str]], tools: list[ToolDefinition], max_output_tokens: int) -> ProviderReply:
        provider_messages: list[dict[str, Any]] = [dict(message) for message in messages]
        request: dict[str, Any] = {
            "model": self.model,
            "system": instructions,
            "messages": provider_messages,
            "max_tokens": max_output_tokens,
        }
        if tools:
            request.update(
                tools=self._tools(tools),
                tool_choice={"type": "auto", "disable_parallel_tool_use": True},
            )
        response = self._client.messages.create(**request)
        return self._reply(response, provider_messages)

    def continue_with(self, reply: ProviderReply, results: list[tuple[ToolCall, dict[str, Any]]], *, instructions: str, tools: list[ToolDefinition], max_output_tokens: int) -> ProviderReply:
        import json

        messages = list(reply.state or [])
        messages.append(
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": call.id,
                        "content": json.dumps(result),
                    }
                    for call, result in results
                ],
            }
        )
        request: dict[str, Any] = {
            "model": self.model,
            "system": instructions,
            "messages": messages,
            "max_tokens": max_output_tokens,
        }
        if tools:
            request.update(
                tools=self._tools(tools),
                tool_choice={"type": "auto", "disable_parallel_tool_use": True},
            )
        response = self._client.messages.create(**request)
        return self._reply(response, messages)


class CliAgentProvider:
    """Drive a subscription-backed coding CLI (Claude Code / Codex) headless.

    No API key is involved: the CLI reuses the operator's logged-in
    subscription. Each turn renders the full conversation plus a tool
    manifest into one prompt and demands a strict JSON decision:
    {"answer": "..."} or {"tool": name, "arguments": {...}}. The student
    agent's approval gates stay authoritative — the CLI can only request
    one tool call per turn and never executes anything itself.
    """

    def __init__(
        self,
        provider_id: str,
        model: str,
        *,
        timeout: float | None = None,
        runner: Callable[[list[str]], Any] | None = None,
    ):
        if provider_id not in CLI_CONFIG:
            raise ValueError("CLI provider must be claude-code or codex-cli")
        self.provider_id = provider_id
        self.name = provider_id
        self.model = model
        self.binary = _cli_binary(provider_id)
        self.timeout = float(timeout or os.environ.get("PIPELINE_AGENT_CLI_TIMEOUT", "180"))
        self._runner = runner

    # -- prompt rendering -------------------------------------------------

    @staticmethod
    def _render_tools(tools: list[ToolDefinition]) -> str:
        if not tools:
            return "(no tools available)"
        blocks = []
        for tool in tools:
            blocks.append(
                f"- {tool.name}: {tool.description}\n  parameters: {json.dumps(tool.parameters)}"
            )
        return "\n".join(blocks)

    @staticmethod
    def _render_history(state: list[dict[str, str]]) -> str:
        lines = []
        for item in state:
            role = item["role"]
            prefix = {"user": "User", "assistant": "Assistant", "tool_result": "Tool result"}.get(role, role)
            lines.append(f"{prefix}: {item['content']}")
        return "\n\n".join(lines) if lines else "(conversation start)"

    def _prompt(self, instructions: str, state: list[dict[str, str]], tools: list[ToolDefinition]) -> str:
        return (
            f"{instructions}\n\n"
            "## Conversation so far\n"
            f"{self._render_history(state)}\n\n"
            "## Tools you may use\n"
            f"{self._render_tools(tools)}\n\n"
            "## Response contract\n"
            "Reply with EXACTLY one JSON object and nothing else — no markdown "
            "fences, no commentary:\n"
            '{"answer": "<your reply to the user>"} to respond directly, OR\n'
            '{"tool": "<tool name>", "arguments": {...}} to call exactly one tool.'
        )

    # -- CLI invocation ---------------------------------------------------

    def _command(self, prompt: str) -> list[str]:
        if self.provider_id == "claude-code":
            return [self.binary, "-p", prompt, "--output-format", "text"]
        return [self.binary, "exec", "--skip-git-repo-check", prompt]

    def _invoke(self, command: list[str], stdin: str | None = None) -> str:
        try:
            if self._runner is not None:
                completed = self._runner(command if stdin is None else [*command, stdin])
            elif stdin is not None:
                completed = subprocess.run(
                    command, input=stdin, capture_output=True, text=True, encoding="utf-8",
                    errors="replace", timeout=self.timeout, cwd=tempfile.gettempdir(),
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            else:
                completed = subprocess.run(command, capture_output=True, text=True, timeout=self.timeout)
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError(f"{self.provider_id} CLI could not start: {exc}") from exc
        if getattr(completed, "returncode", 1) != 0:
            stderr = (getattr(completed, "stderr", "") or "").strip()
            raise RuntimeError(f"{self.provider_id} CLI failed: {stderr[:300] or 'non-zero exit'}")
        return (getattr(completed, "stdout", "") or "").strip()

    @staticmethod
    def extract_json(raw: str) -> dict[str, Any]:
        """Pull the first JSON object out of possibly noisy CLI output."""
        text = raw.strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
        start = text.find("{")
        while start != -1:
            depth = 0
            in_string = False
            escaped = False
            for index in range(start, len(text)):
                char = text[index]
                if in_string:
                    if escaped:
                        escaped = False
                    elif char == "\\":
                        escaped = True
                    elif char == '"':
                        in_string = False
                    continue
                if char == '"':
                    in_string = True
                elif char == "{":
                    depth += 1
                elif char == "}":
                    depth -= 1
                    if depth == 0:
                        candidate = text[start : index + 1]
                        try:
                            parsed = json.loads(candidate)
                            if isinstance(parsed, dict):
                                return parsed
                        except json.JSONDecodeError:
                            break
                        break
            start = text.find("{", start + 1)
        raise ValueError(f"CLI reply was not a JSON decision: {raw[:200]}")

    def _decide(self, instructions: str, state: list[dict[str, str]], tools: list[ToolDefinition], max_output_tokens: int) -> ProviderReply:
        output = self._invoke(self._command(self._prompt(instructions, state, tools)))
        decision = self.extract_json(output)
        request_id = f"cli-{hashlib.sha256(output.encode()).hexdigest()[:12]}"
        if isinstance(decision.get("tool"), str):
            arguments = decision.get("arguments") or {}
            if not isinstance(arguments, dict):
                arguments = {}
            return ProviderReply(
                text="",
                tool_calls=[ToolCall(id=f"call-{request_id}", name=str(decision["tool"]), arguments=arguments)],
                request_id=request_id,
                state=[*state],
            )
        return ProviderReply(
            text=str(decision.get("answer", "")),
            request_id=request_id,
            input_tokens=0,
            output_tokens=max(1, len(output) // 4),
            state=[*state],
        )

    def complete_text(self, instructions: str, content: str) -> str:
        """One tool-free completion with no response contract of its own.

        The agent contract wraps every reply in {"answer": ...}; callers that
        need their own JSON shape use this instead. The prompt goes over stdin,
        the CLI gets no tools or MCP servers, and it runs outside the project
        directory, so text taken from the web cannot steer it into local files.
        """
        prompt = f"{instructions}\n\n{content}"
        if self.provider_id == "claude-code":
            command = [self.binary, "-p", "--output-format", "text", "--tools", "", "--strict-mcp-config"]
        else:
            command = [self.binary, "exec", "--skip-git-repo-check", "--sandbox", "read-only", "-"]
        return self._invoke(command, stdin=prompt)

    # -- AgentProvider protocol -------------------------------------------

    def create(self, *, instructions: str, messages: list[dict[str, str]], tools: list[ToolDefinition], max_output_tokens: int) -> ProviderReply:
        state = [{"role": str(message["role"]), "content": str(message["content"])} for message in messages]
        return self._decide(instructions, state, tools, max_output_tokens)

    def continue_with(self, reply: ProviderReply, results: list[tuple[ToolCall, dict[str, Any]]], *, instructions: str, tools: list[ToolDefinition], max_output_tokens: int) -> ProviderReply:
        state = list(reply.state or [])
        for call, result in results:
            state.append({"role": "tool_result", "content": f"{call.name} -> {json.dumps(result)}"})
        return self._decide(instructions, state, tools, max_output_tokens)


def complete_text(provider: AgentProvider, instructions: str, content: str, *, max_output_tokens: int = 1200) -> str:
    """Single-shot text completion across API and CLI providers, without tools."""
    direct = getattr(provider, "complete_text", None)
    if callable(direct):
        return str(direct(instructions, content))
    reply = provider.create(
        instructions=instructions,
        messages=[{"role": "user", "content": content}],
        tools=[],
        max_output_tokens=max_output_tokens,
    )
    return reply.text


def build_provider(provider: str, model: str) -> AgentProvider:
    configured = configured_provider(provider)
    if model != configured["model"]:
        raise ValueError("The configured model changed; start a new thread to use it")
    if provider == "openai":
        return OpenAIProvider(model)
    if provider == "anthropic":
        return AnthropicProvider(model)
    return CliAgentProvider(provider, model)
