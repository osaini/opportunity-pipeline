"""Reading one person's LinkedIn profile, through the student's own LinkedIn test account.

Call prep reads the interviewer's profile so the student can ask about the
interviewer's own path (outreach_interviewer.py). LinkedIn is behind a login,
so this goes through ``mcp-server-linkedin`` run by ``mcporter``, signed in as
an account the student set up for this (SETUP.md step 7b, Call prep),
never the account in their everyday browser. Every read checks, first:

- ``PIPELINE_LINKEDIN_ACCOUNT`` names the account this may use (its profile
  URL or username); empty means LinkedIn is off;
- the server's mcporter entry keeps browser cookie import off
  (``--no-auto-import`` and ``AUTO_IMPORT_FROM_BROWSER=false``, and never
  ``--import-from-browser``), so it cannot pick up the browser's account;
- the account signed in is that one (``get_my_profile``).

Anything else and nothing is read. Only reading tools are ever called
(READ_TOOLS): never messages, connection requests, or anything that writes.
Calls are spaced MIN_GAP apart across the whole app, to be gentle on the
account.

mcporter is run as node and its cli.js, never through a ``.cmd`` or ``.bat``
shim: Windows would hand the arguments, which carry a person's name, to
cmd.exe, and a name with an ``&`` in it would run as a command.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

ACCOUNT_ENV = "PIPELINE_LINKEDIN_ACCOUNT"
MCPORTER_ENV = "PIPELINE_MCPORTER"
SERVER = "linkedin-scraper"
READ_TOOLS = frozenset({"get_my_profile", "search_people", "get_person_profile"})
# Profile sections read for call prep: where they worked, what they studied, what they post about.
PROFILE_SECTIONS = "experience,education,posts"
MIN_GAP = 20.0
CALL_TIMEOUT_SECONDS = 300
CONFIG_TIMEOUT_SECONDS = 60

Call = Callable[[str, dict[str, Any]], dict[str, Any]]

_pace_lock = threading.Lock()
_last_call = [0.0]


class LinkedInUnavailable(RuntimeError):
    """LinkedIn is off, not set up, or signed in as an account this may not use."""


def username_from(value: str) -> str:
    """"linkedin.com/in/jane-doe-123/" and "jane-doe-123" are both "jane-doe-123"."""
    text = str(value or "").strip()
    if "/" in text:
        path = urlsplit(text if "//" in text else f"https://{text}").path
        match = re.search(r"/in/([^/?#]+)", path)
        text = match.group(1) if match else ""
    return text.strip().strip("/").casefold()


def configured_account() -> str:
    return username_from(os.environ.get(ACCOUNT_ENV, ""))


# What cmd.exe reads as more than text: a search for a name with one of these in it would run as a command.
CMD_META = re.compile(r'[&|<>^%!"`\r\n]')
_SHIM_TARGET = re.compile(r"""["']?((?:%~?dp0%?|\$basedir)?[^"'\s]*mcporter[^"'\s]*?cli\.js)""", re.IGNORECASE)


def _shim_script(shim: Path) -> Path | None:
    """The cli.js a .cmd shim starts, read from the shim itself (npm's, pnpm's, yarn's), or None. The shim is never run."""
    try:
        text = shim.read_text(encoding="utf-8", errors="replace")[:20_000]
    except OSError:
        return None
    for match in _SHIM_TARGET.finditer(text):
        raw = re.sub(r"%~?dp0%?|\$basedir", lambda _match: str(shim.parent), match.group(1), flags=re.IGNORECASE).replace("\\", "/")
        script = Path(raw)
        if script.name == "cli.js" and script.exists():
            return script.resolve()
    return None


def mcporter_command() -> list[str]:
    """How to run mcporter: node and its cli.js, so no shell parses the arguments; refuses a .cmd or .bat shim it cannot see through."""
    configured = os.environ.get(MCPORTER_ENV, "").strip()
    found = configured or shutil.which("mcporter") or ""
    if not found:
        fallback = Path(os.environ.get("APPDATA", "")) / "fnm" / "aliases" / "default" / "mcporter"
        found = str(fallback) if fallback.parent.exists() else ""
    if not found:
        raise LinkedInUnavailable("mcporter is not installed here, so LinkedIn cannot be read (SETUP.md step 7b, Call prep)")
    folder = Path(found).parent
    script = folder / "node_modules" / "mcporter" / "dist" / "cli.js"
    if not script.exists() and Path(found).suffix.casefold() in {".cmd", ".bat"}:
        script = _shim_script(Path(found)) or script
    if script.exists():
        node = folder / ("node.exe" if os.name == "nt" else "node")
        interpreter = str(node) if node.exists() else (shutil.which("node") or "")
        # Never a .cmd or .bat (node.cmd from a version manager): run with arguments that carry a name, cmd.exe would read them.
        if not interpreter or Path(interpreter).suffix.casefold() in {".cmd", ".bat", ".ps1"}:
            raise LinkedInUnavailable(
                f"node.exe was not found for mcporter{f' (only {interpreter})' if interpreter else ''}, and a .cmd or .bat file is never "
                f"run with a person's name as an argument, so LinkedIn is not read. Set {MCPORTER_ENV} to a mcporter installed beside node.exe"
            )
        return [interpreter, str(script)]
    if Path(found).suffix.casefold() in {".cmd", ".bat"}:
        raise LinkedInUnavailable(
            f"mcporter is a {Path(found).suffix} shim ({found}) and its cli.js was not found next to it, so LinkedIn is not read "
            f"(a shim would pass a person's name to cmd.exe). Install mcporter with npm -g, or set {MCPORTER_ENV} to its folder's mcporter"
        )
    return [found]


def _run(arguments: list[str], timeout: float) -> str:
    try:
        completed = subprocess.run(
            [*mcporter_command(), *arguments], capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"mcporter did not answer within {int(timeout)} seconds") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise RuntimeError(f"mcporter exited {completed.returncode}: {detail[-400:] or 'no output'}")
    return completed.stdout


def mcporter_call(tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    raw = _run(["call", f"{SERVER}.{tool}", "--args", json.dumps(arguments), "--output", "json",
                "--timeout", str(CALL_TIMEOUT_SECONDS * 1000)], CALL_TIMEOUT_SECONDS + 30)
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise RuntimeError(f"LinkedIn gave an answer that is not JSON: {raw[:200]}") from exc


def mcporter_config() -> str:
    return _run(["config", "get", SERVER], CONFIG_TIMEOUT_SECONDS)


def config_problem(config: str) -> str:
    """Why the server's entry could let it use the browser's account, or ""."""
    if "--import-from-browser" in config:
        return "the LinkedIn server is set to import the browser's sign-in"
    if "--no-auto-import" not in config or "AUTO_IMPORT_FROM_BROWSER=false" not in config:
        return "the LinkedIn server's entry does not turn off importing the browser's sign-in (--no-auto-import and AUTO_IMPORT_FROM_BROWSER=false)"
    return ""


class LinkedInClient:
    """Read-only LinkedIn calls, each made only after the account checks pass."""

    def __init__(
        self,
        *,
        call: Call = mcporter_call,
        config: Callable[[], str] = mcporter_config,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        min_gap: float = MIN_GAP,
    ) -> None:
        self._call = call
        self._config = config
        self._sleep = sleep
        self._clock = clock
        self._min_gap = min_gap
        self._checked = ""

    def _paced(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if tool not in READ_TOOLS:
            raise LinkedInUnavailable(f"{tool} is not a reading call, and this only reads")
        with _pace_lock:
            wait = _last_call[0] + self._min_gap - self._clock()
            if _last_call[0] and wait > 0:
                self._sleep(wait)
            try:
                return self._call(tool, arguments)
            finally:
                _last_call[0] = self._clock()

    def check_account(self) -> str:
        """The account's username once it is the configured one, signed in with browser import off."""
        wanted = configured_account()
        if not wanted:
            raise LinkedInUnavailable(f"LinkedIn is off: no account is set in {ACCOUNT_ENV}")
        if self._checked == wanted:
            return wanted
        problem = config_problem(self._config())
        if problem:
            raise LinkedInUnavailable(f"Nothing was read from LinkedIn: {problem}")
        signed_in = username_from(str(self._paced("get_my_profile", {}).get("url") or ""))
        if signed_in != wanted:
            raise LinkedInUnavailable(
                f"Nothing was read from LinkedIn: it is signed in as {signed_in or 'no one'}, not {wanted}"
            )
        self._checked = wanted
        return wanted

    def search_people(self, keywords: str) -> list[dict[str, str]]:
        """People LinkedIn finds for these words: username, name, and the text of their result."""
        self.check_account()
        # A name is text to search for; nothing in it is a command, whatever ran it.
        keywords = " ".join(CMD_META.sub(" ", str(keywords or "")).split())
        if not keywords:
            return []
        answer = self._paced("search_people", {"keywords": keywords})
        text = str((answer.get("sections") or {}).get("search_results") or "")
        people = []
        for item in (answer.get("references") or {}).get("search_results") or []:
            if not isinstance(item, dict) or item.get("kind") != "person":
                continue
            username = username_from(str(item.get("url") or ""))
            if username:
                people.append({"username": username, "name": " ".join(str(item.get("text") or "").split()), "text": text})
        return people

    def profile(self, username: str) -> dict[str, Any]:
        """One person's profile: its URL and the text of each section read."""
        self.check_account()
        answer = self._paced("get_person_profile", {"linkedin_username": username, "sections": PROFILE_SECTIONS, "max_scrolls": 2})
        sections = {key: str(value) for key, value in (answer.get("sections") or {}).items() if isinstance(value, str) and value.strip()}
        if not sections:
            raise RuntimeError(f"LinkedIn returned nothing for {username}")
        return {"url": str(answer.get("url") or f"https://www.linkedin.com/in/{username}/"), "username": username, "sections": sections}
