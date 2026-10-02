"""The headless CLI agents that research companies on the web, and which one the deep search uses.

Deep search, location search, other-sites email search, recontact and company
research each hand a prompt to one of two command-line agents, Claude Code or
Codex CLI, and read its text reply. A runner takes the prompt, runs the agent
outside the project, and returns what it printed: Claude Code with only web
search and fetch, Codex with web search too but only when the student has
accepted that in .env (see ``codex_runner``). ``RUNNERS`` maps the provider name the student chose to its runner.
It is one dict object: the outreach CLI imports it, and tests patch its
entries in place.
"""

from __future__ import annotations

import tempfile
from typing import Callable

from ..integrations.agent_providers import (
    CodexNotIsolated, cli_binary, codex_command, codex_failure_detail, failure_detail, run_headless,
)
from .config import ALLOW_CODEX_ENV, codex_web_allowed, discovery_provider

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
    """Fallback: Codex CLI with web search, in an empty directory outside the project. Refused unless the student opted in.

    Codex has no web-search-only mode like Claude's. Its web tool comes with code mode, which also exposes apply_patch:
    the read-only sandbox stops it writing, but not testing whether a local file holds given lines. Every other Codex
    call here has no tools at all; this one cannot, and it reads pages nobody vetted. So it raises CodexNotIsolated,
    naming the way out, unless ALLOW_CODEX_ENV is set in .env. Shell, MCP servers and file writes stay off regardless
    (agent_providers.codex_command).
    """
    if not codex_web_allowed():
        raise CodexNotIsolated(
            "Codex was not used for web research: it cannot be limited to web search, and the pages it reads are not "
            f"trusted. Choose Claude Code for this, or set {ALLOW_CODEX_ENV}=1 in .env to accept that."
        )
    command = codex_command(cli_binary("codex-cli"), web_search=True)
    with tempfile.TemporaryDirectory(prefix="outreach-discovery-") as workdir:
        completed = run_headless(command, prompt, timeout=timeout, cwd=workdir)
    if completed.returncode != 0:
        detail = codex_failure_detail(completed)
        raise RuntimeError(f"Codex exited {completed.returncode}: {detail[-400:] or 'no output'}")
    return completed.stdout


RUNNERS: dict[str, Runner] = {"claude-code": claude_runner, "codex-cli": codex_runner}


def discovery_runner() -> Runner:
    """The runner for the deep search's provider setting: Claude Code when none is set, and for a name nothing runs."""
    return RUNNERS.get(discovery_provider(), claude_runner)
