"""The headless CLI agents that research companies on the web, and which one the deep search uses.

Deep search, location search, other-sites email search, recontact and company
research each hand a prompt to one of two command-line agents, Claude Code or
Codex CLI, and read its text reply. A runner takes the prompt, runs the agent
outside the project with only web search and fetch, and returns what it
printed. ``RUNNERS`` maps the provider name the student chose to its runner.
It is one dict object: the outreach CLI imports it, and tests patch its
entries in place.
"""

from __future__ import annotations

import tempfile
from typing import Callable

from .integrations.agent_providers import CODEX_READ_ONLY, cli_binary, failure_detail, run_headless
from .outreach_config import discovery_provider

Runner = Callable[[str], str]

RUNNER_TIMEOUT_SECONDS = 45 * 60


def claude_runner(prompt: str, *, timeout: float = RUNNER_TIMEOUT_SECONDS) -> str:
    """Headless Claude Code with web search and fetch only, outside the project."""
    command = [
        cli_binary("claude-code"), "-p", "--output-format", "text",
        "--tools", "WebSearch,WebFetch", "--allowedTools", "WebSearch,WebFetch",
        "--strict-mcp-config",
    ]
    with tempfile.TemporaryDirectory(prefix="outreach-discovery-") as workdir:
        completed = run_headless(command, prompt, timeout=timeout, cwd=workdir)
    if completed.returncode != 0:
        detail = failure_detail(completed)
        raise RuntimeError(f"Claude Code exited {completed.returncode}: {detail[:400] or 'no output'}")
    return completed.stdout


def codex_runner(prompt: str, *, timeout: float = RUNNER_TIMEOUT_SECONDS) -> str:
    """Fallback: Codex CLI with web search, read-only sandbox, outside the project."""
    command = [cli_binary("codex-cli"), *CODEX_READ_ONLY, "-c", "tools.web_search=true", "-"]
    with tempfile.TemporaryDirectory(prefix="outreach-discovery-") as workdir:
        completed = run_headless(command, prompt, timeout=timeout, cwd=workdir)
    if completed.returncode != 0:
        detail = failure_detail(completed)
        raise RuntimeError(f"Codex exited {completed.returncode}: {detail[-400:] or 'no output'}")
    return completed.stdout


RUNNERS: dict[str, Runner] = {"claude-code": claude_runner, "codex-cli": codex_runner}


def discovery_runner() -> Runner:
    """The runner for the deep search's provider setting: Claude Code when none is set, and for a name nothing runs."""
    return RUNNERS.get(discovery_provider(), claude_runner)
