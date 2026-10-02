"""Provider-neutral model and tool-call adapters for the student agent."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import tomllib
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Protocol


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


# Inside catalog_snapshot(): [the catalog built by the first provider_catalog() call there, or None before it].
_catalog_snapshot: ContextVar[list[Any] | None] = ContextVar("provider_catalog_snapshot", default=None)


@contextmanager
def catalog_snapshot() -> Iterator[None]:
    """Let one request build the provider catalog once, however many helpers ask for it.

    Building it scans PATH for each CLI, and one Outreach settings load asks
    for it a dozen times (the writer, the reviewer twice, each one's default).
    Inside this block the first provider_catalog() call builds it and the rest
    get copies. The snapshot lasts only for the block and only in this context
    (thread), so nothing is cached between requests: a CLI installed a moment
    later shows on the next one. Patching provider_catalog still wins.
    """
    if _catalog_snapshot.get() is not None:
        yield
        return
    token = _catalog_snapshot.set([None])
    try:
        yield
    finally:
        _catalog_snapshot.reset(token)


def provider_catalog() -> list[dict[str, Any]]:
    """Return safe provider metadata; secrets never leave the server."""

    snapshot = _catalog_snapshot.get()
    if snapshot is None:
        return _build_provider_catalog()
    if snapshot[0] is None:
        snapshot[0] = _build_provider_catalog()
    return [dict(item) for item in snapshot[0]]


def _build_provider_catalog() -> list[dict[str, Any]]:
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
            cli_available(cli_binary("claude-code")),
            "Install Claude Code and log in (`claude`) to use your Claude subscription.",
        ),
        (
            "codex-cli",
            "Codex CLI (subscription)",
            os.environ.get("CODEX_AGENT_MODEL", "subscription"),
            cli_available(cli_binary("codex-cli")),
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


def cli_binary(provider_id: str) -> str:
    binary, override_env = CLI_CONFIG[provider_id]
    return os.environ.get(override_env) or binary


def cli_available(binary: str) -> bool:
    return bool(shutil.which(binary))


# The Claude Code flags for a call with no tools and no MCP servers (a reviewer, or complete_text).
CLAUDE_NO_TOOLS = ["-p", "--output-format", "text", "--tools", "", "--strict-mcp-config"]

# Codex has no single "no tools" switch like Claude's, so a Codex call is isolated by several settings together. The app
# builds every Codex argv with codex_command, and run_headless refuses one that lacks any of them (require_codex_isolation).
#
# What each setting closes (checked against codex-cli 0.157.0 and 0.159.2 by asking the model to list its tools):
#   --ignore-user-config, --ignore-rules   ~/.codex/config.toml, its MCP servers, hooks, profiles and exec-policy rules are
#                                          not loaded (auth still comes from CODEX_HOME, which this app never reads or copies)
#   -c mcp_servers={}                      no MCP server, whatever else configures one
#   --sandbox read-only                    the second layer: nothing the model runs can write
#   --disable shell_tool, unified_exec     no command-running tool
#   --disable code_mode_host               "code mode" fails closed for the models whose catalog entry selects it
#                                          (code_mode_only: the GPT-5.6 and GPT-6 families). It is what carries the web tool and
#                                          the clock, and, for those models, apply_patch
#   CODEX_EXEC_SERVER_URL=none (env var)   no environment: Codex registers apply_patch (and the file and image tools) only
#                                          when the turn has one. See CODEX_NO_ENVIRONMENT
#   -c web_search=disabled                 no web search
#   -c shell_environment_policy.inherit=none   a command that somehow ran would see none of this app's environment
#   --ephemeral                            no session files are left in CODEX_HOME
#   --strict-config, --disable <name>      an unknown setting or feature is an error, so a Codex that does not know one of
#                                          these refuses to start rather than starting with a tool the app meant to turn off
# Not closed: nothing the model can reach. multi_agent is off, so there is no spawn_agent to start another model with.
# What still lists: request_user_input and multi_tool_use.parallel, which read no file (both versions, gpt-5.5).
#
# Why the environment and not a setting for apply_patch: in Codex 0.157.0 and 0.159.2 (core/src/tools/spec_plan.rs) the
# tool is registered when the turn has an environment AND the model's catalog entry has apply_patch_tool_type, which every
# bundled entry does. No config key or feature overrides that (there is no include_apply_patch_tool any more, and
# with_config_overrides in models-manager leaves the field alone), and for a Direct-mode model (tool_mode unset, e.g. gpt-5.5)
# it is a top-level tool that code_mode_host does not touch. CODEX_EXEC_SERVER_URL=none (exec-server/src/environment.rs) is
# the one switch that removes it for every model. It is an environment variable, so --strict-config cannot vouch for it:
# run_headless sets it itself (codex_process_env) and require_codex_isolation checks the environment it was handed.
CODEX_OFF_FEATURES = (
    "shell_tool", "unified_exec", "plugins", "apps", "multi_agent", "browser_use", "computer_use", "in_app_browser",
    "view_image", "image_generation", "goals", "memories", "hooks", "skill_search", "tool_suggest", "sleep_tool",
)
CODEX_CODE_MODE = "code_mode_host"
# The process environment every Codex call without web search gets: no environment, so no apply_patch for any model.
CODEX_EXEC_SERVER_ENV = "CODEX_EXEC_SERVER_URL"
CODEX_NO_ENVIRONMENT = {CODEX_EXEC_SERVER_ENV: "none"}
_ENV_UNCHECKED = object()
_CODEX_SWITCHES = ("--skip-git-repo-check", "--ignore-user-config", "--ignore-rules", "--ephemeral", "--strict-config")
# Values carry no quotes: a quote on argv does not survive a cmd.exe shim (codex.cmd), and Codex reads a value that is
# not valid TOML as a plain string.
_CODEX_OVERRIDES = ("mcp_servers={}", "shell_environment_policy.inherit=none")
_CODEX_WEB_SEARCH = "web_search="
_CODEX_EFFORT = "model_reasoning_effort="
# The student's .env opt-in for the one Codex call that carries a web tool (see codex_command). outreach.config re-exports it.
CODEX_WEB_OPT_IN_ENV = "PIPELINE_OUTREACH_RESEARCH_ALLOW_CODEX"
CODEX_MODEL_ENV = "PIPELINE_CODEX_MODEL"
CODEX_EFFORT_ENV = "PIPELINE_CODEX_REASONING_EFFORT"
CODEX_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh")
_CODEX_MODEL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,63}")
# The only options a Codex argv may carry. Anything else (a looser sandbox, a profile, another directory, a `-c` that
# turns a feature back on) is refused, whatever form it is written in. Each flag below takes one value except the switches.
_CODEX_VALUE_FLAGS = ("--sandbox", "-c", "--disable", "-m", "--output-last-message")


class CodexNotIsolated(RuntimeError):
    """A Codex command that lacks the isolation, or a Codex call the app refuses to make. Nothing was started."""


def opt_in_value_is_on(value: str | None) -> bool:
    """Whether an opt-in setting's text turns it on: 1, true, yes or on, in any case. Anything else, '0' included, is off."""
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def codex_web_opted_in() -> bool:
    """Whether the student accepted, in .env, that Codex reads web pages for research (CODEX_WEB_OPT_IN_ENV)."""
    return opt_in_value_is_on(os.environ.get(CODEX_WEB_OPT_IN_ENV))


def codex_model_settings() -> tuple[str, str]:
    """The model and reasoning effort Codex runs with: the student's own (.env, else Codex's config.toml), or "" for each.

    ``--ignore-user-config`` stops Codex reading config.toml, which would otherwise silently drop the model and effort the
    student chose, so they are read here (these two keys only, nothing else in that file) and passed on the command line.
    A value that is not a plain model name or a known effort is ignored, so the file cannot put an option on argv.
    """
    model = os.environ.get(CODEX_MODEL_ENV, "").strip()
    effort = os.environ.get(CODEX_EFFORT_ENV, "").strip().lower()
    if not (model and effort):
        try:
            home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
            with open(home / "config.toml", "rb") as handle:
                configured = tomllib.load(handle)
        except (OSError, ValueError):
            configured = {}
        file_model, file_effort = configured.get("model"), configured.get("model_reasoning_effort")
        model = model or (file_model if isinstance(file_model, str) else "").strip()
        effort = effort or (file_effort if isinstance(file_effort, str) else "").strip().lower()
    return (model if _CODEX_MODEL_NAME.fullmatch(model) else ""), (effort if effort in CODEX_EFFORTS else "")


def _codex_flags(web_search: bool) -> list[str]:
    flags = ["exec", "--sandbox", "read-only", *_CODEX_SWITCHES]
    model, effort = codex_model_settings()
    if model:
        flags += ["-m", model]
    if effort:
        flags += ["-c", f"{_CODEX_EFFORT}{effort}"]
    for override in (*_CODEX_OVERRIDES, f"{_CODEX_WEB_SEARCH}{'live' if web_search else 'disabled'}"):
        flags += ["-c", override]
    for feature in (*CODEX_OFF_FEATURES, *(() if web_search else (CODEX_CODE_MODE,))):
        flags += ["--disable", feature]
    return flags


def codex_command(binary: str, *, web_search: bool = False, extra: tuple[str, ...] = ()) -> list[str]:
    """The argv for one isolated Codex call: no MCP servers, command tool, file writes or web search, prompt on stdin.

    ``web_search=True`` is for the company-research runner alone. Codex reaches the web through code mode, which cannot be
    switched on without also exposing apply_patch (read-only keeps it from writing, but it can still test whether a local
    file holds given lines), so that one call is isolated less than the rest and its caller must have the student's opt-in.
    Every other call also needs the environment from codex_process_env, which run_headless supplies: argv alone cannot take
    apply_patch away from a model whose catalog entry lists it directly. ``extra`` goes before the final "-".
    """
    return [binary, *_codex_flags(web_search), *extra, "-"]


def _is_web_research(args: list[str]) -> bool:
    return f"{_CODEX_WEB_SEARCH}live" in _values_after(args, "-c")


def codex_process_env(command: list[str], base: dict[str, str] | None = None) -> dict[str, str] | None:
    """The environment to start a command with: for a Codex call without web search, ``base`` (the app's) with no
    environment for Codex (CODEX_NO_ENVIRONMENT) and no other CODEX_EXEC_SERVER_* setting; None (inherit) otherwise.

    Without an environment Codex registers neither apply_patch nor any other file tool, whatever model is chosen. The
    company-research call keeps the inherited environment: it is the one call that has apply_patch (see codex_command).
    """
    if not _is_codex(command) or _is_web_research([str(item) for item in command[1:]]):
        return None
    inherited = os.environ if base is None else base
    env = {key: value for key, value in inherited.items() if not key.upper().startswith("CODEX_EXEC_SERVER_")}
    return {**env, **CODEX_NO_ENVIRONMENT}


def _values_after(args: list[str], flag: str) -> list[str]:
    return [args[index + 1] for index, item in enumerate(args[:-1]) if item == flag]


def _is_codex(command: list[str]) -> bool:
    return bool(command) and (os.path.basename(str(command[0])).lower().startswith("codex") or command[0] == cli_binary("codex-cli"))


def _unlisted_codex_options(args: list[str]) -> list[str]:
    """Problems for every option in a Codex argv (after `exec`) that codex_command could not have written.

    Walks the argv the way Codex reads it: a known switch, or a known flag with its one value, and a final "-" for the
    prompt on stdin. A `-c` may carry only the settings codex_command sets, plus a valid reasoning effort, so an override
    that re-enables an MCP server, a sandbox, a web search or a profile is refused, as are `--config`, `-p`, `--add-dir`
    and the `--flag=value` and `-cvalue` spellings of anything. `web_search=live` is allowed only while the student's
    opt-in is set, the one condition under which the research runner builds it.
    """
    problems = []
    allowed_overrides = (*_CODEX_OVERRIDES, f"{_CODEX_WEB_SEARCH}disabled",
                         *((f"{_CODEX_WEB_SEARCH}live",) if codex_web_opted_in() else ()),
                         *(f"{_CODEX_EFFORT}{effort}" for effort in CODEX_EFFORTS))
    index = 1
    while index < len(args):
        item = args[index]
        if item in _CODEX_SWITCHES:
            index += 1
        elif item in _CODEX_VALUE_FLAGS:
            if index + 1 >= len(args):
                problems.append(f"{item} has no value")
                break
            value = args[index + 1]
            if item == "-c" and value not in allowed_overrides:
                problems.append(f"-c {value} is not allowed")
            elif item == "-m" and not _CODEX_MODEL_NAME.fullmatch(value):
                problems.append(f"-m {value} is not a model name")
            index += 2
        elif item == "-" and index == len(args) - 1:
            index += 1
        else:
            problems.append(f"{item} is not allowed")
            index += 1
    if args[-1:] != ["-"]:
        problems.append("the prompt must go over stdin (a final -)")
    return problems


def require_codex_isolation(command: list[str], env: Any = _ENV_UNCHECKED) -> None:
    """Raise CodexNotIsolated, before anything starts, when a Codex command lacks any part of codex_command's isolation.

    ``env`` is the environment the process will start with (None: the inherited one). When it is given, a call without web
    search must carry CODEX_NO_ENVIRONMENT in it, the part of the isolation that is not on argv (see codex_process_env).
    run_headless always passes it; the check run before an injected runner does not know it and leaves it out.
    Commands for other programs pass untouched.
    """
    if not _is_codex(command):
        return
    args = [str(item) for item in command[1:]]
    problems = []
    if args[:1] != ["exec"]:
        problems.append("it is not `codex exec`")
    problems += _unlisted_codex_options(args)
    problems += [f"{switch} is missing" for switch in _CODEX_SWITCHES if switch not in args]
    if _values_after(args, "--sandbox") != ["read-only"]:
        problems.append("--sandbox must be given once, as read-only")
    overrides = _values_after(args, "-c")
    problems += [f"-c {override} is missing" for override in _CODEX_OVERRIDES if override not in overrides]
    web = [item for item in overrides if item.startswith(_CODEX_WEB_SEARCH)]
    if len(web) != 1 or web[0] not in (f"{_CODEX_WEB_SEARCH}disabled", f"{_CODEX_WEB_SEARCH}live"):
        problems.append("-c web_search must be set once, to disabled or live")
    off = _values_after(args, "--disable")
    wanted = (*CODEX_OFF_FEATURES, *(() if web == [f"{_CODEX_WEB_SEARCH}live"] else (CODEX_CODE_MODE,)))
    problems += [f"--disable {feature} is missing" for feature in wanted if feature not in off]
    if env is not _ENV_UNCHECKED and not _is_web_research(args):
        if (env or {}).get(CODEX_EXEC_SERVER_ENV) != CODEX_NO_ENVIRONMENT[CODEX_EXEC_SERVER_ENV]:
            problems.append(f"{CODEX_EXEC_SERVER_ENV}=none is missing from its environment, so apply_patch would be listed for some models")
    if problems:
        raise CodexNotIsolated(
            "Codex was not started because its command is not isolated: " + "; ".join(problems) + ". "
            "Every Codex call is built with agent_providers.codex_command."
        )


def run_headless(command: list[str], prompt: str, *, timeout: float, cwd: str) -> subprocess.CompletedProcess:
    """Run a Claude Code or Codex CLI command with the prompt on stdin, and return what it did.

    Only the invocation is shared: UTF-8 with undecodable bytes replaced, no console window on Windows. Each caller
    chooses its own directory (an empty one, outside the project), checks the return code and words its own error,
    because what a student sees on a failure differs per caller. A timeout or a missing binary raises as subprocess does.
    A Codex command that lacks the isolation of codex_command raises CodexNotIsolated without starting anything. A Codex
    call without web search starts with the environment of codex_process_env (no apply_patch for any model).
    """
    env = codex_process_env(command)
    require_codex_isolation(command, env)
    return subprocess.run(
        command, input=prompt, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout, cwd=cwd, env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def failure_detail(completed: subprocess.CompletedProcess) -> str:
    """What a failed CLI said: its stderr, else its stdout, trimmed."""
    return (completed.stderr or completed.stdout or "").strip()


_CODEX_TOO_OLD = ("unexpected argument", "unknown feature flag", "unknown configuration field")


def codex_update_hint(detail: str) -> str:
    """A sentence to add to a failed Codex call's message when the CLI did not understand a setting of the isolation."""
    if any(phrase in detail.lower() for phrase in _CODEX_TOO_OLD):
        return " (This Codex CLI does not understand a setting the app needs to keep it isolated. Update it with `codex update`.)"
    return ""


def codex_failure_detail(completed: subprocess.CompletedProcess) -> str:
    """failure_detail for a Codex call, with the update hint when the CLI is too old for the isolation settings."""
    detail = failure_detail(completed)
    return detail + codex_update_hint(detail)


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
    # One catalog for every candidate (configured_provider would build it again for each).
    catalog = {item["id"]: item for item in provider_catalog()}
    for candidate in ("openai", "anthropic", "claude-code", "codex-cli"):
        if candidate in catalog and catalog[candidate]["configured"]:
            return candidate
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
    agent's approval gates stay authoritative: the CLI is asked for one tool
    call per turn and is started with its own tools and MCP servers turned
    off (see _command), so it has nothing to execute a call with.
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
        self.binary = cli_binary(provider_id)
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

    def _command(self) -> list[str]:
        """The isolated CLI invocation shared by every turn and by complete_text.

        The prompt is never part of it: it goes over stdin, so text taken from the web or from tool results cannot be
        re-parsed by a cmd.exe shim. Claude Code gets no tools and no MCP servers (CLAUDE_NO_TOOLS). Codex gets
        codex_command's settings and run_headless's empty Codex environment (codex_process_env), which together leave it no
        MCP server, command tool, web search, apply_patch or file write, with the read-only sandbox as a second layer;
        _invoke refuses a Codex command without the settings. _invoke runs it in a directory
        of its own, empty and outside the project.
        """
        if self.provider_id == "claude-code":
            return [self.binary, *CLAUDE_NO_TOOLS]
        return codex_command(self.binary)

    def _invoke(self, command: list[str], stdin: str) -> str:
        # Checked here as well as in run_headless so an injected runner cannot hide a command that lost its isolation.
        require_codex_isolation(command)
        try:
            if self._runner is not None:
                completed = self._runner([*command, stdin])
            else:
                # A directory of its own and empty, as the sibling CLI runners use, not the shared system temp.
                with tempfile.TemporaryDirectory(prefix="agent-cli-", ignore_cleanup_errors=True) as workdir:
                    completed = run_headless(command, stdin, timeout=self.timeout, cwd=workdir)
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError(f"{self.provider_id} CLI could not start: {exc}") from exc
        if getattr(completed, "returncode", 1) != 0:
            stderr = (getattr(completed, "stderr", "") or "").strip()
            hint = codex_update_hint(stderr) if self.provider_id == "codex-cli" else ""
            raise RuntimeError(f"{self.provider_id} CLI failed: {stderr[:300] or 'non-zero exit'}{hint}")
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
        output = self._invoke(self._command(), stdin=self._prompt(instructions, state, tools))
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
        the CLI runs with the isolation described at _command (Codex: codex_command),
        and in an empty directory outside the project.
        """
        prompt = f"{instructions}\n\n{content}"
        return self._invoke(self._command(), stdin=prompt)

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
