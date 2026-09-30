"""Read the pipeline's Gmail mailbox, read-only: the sanctioned way for an agent to look at outreach mail.

The pipeline mailbox is the account the app's Gmail connection signed into. A
coding agent's own Gmail tool is usually signed into a different account, so use
this instead, and never fall back to that tool when this fails.

    py -3 scripts/pipeline_mailbox.py [--root DIR] [--user USER_ID] whoami
    py -3 scripts/pipeline_mailbox.py search "label:opportunities" [--max N]
    py -3 scripts/pipeline_mailbox.py thread THREAD_ID

(python3 on macOS and Linux.) Run it from the checkout where the app runs; from
a git worktree it finds the main checkout, whose .env and database hold the
connection. It opens that database read-only and only ever makes Gmail GET
requests. What it prints goes into the agent's conversation and to its model
provider. The reading is done by opportunity_app/pipeline_mailbox.py; this file
only picks the checkout and the Python that has the app's packages, and uses the
standard library alone so it starts anywhere.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

CHECKOUT = Path(__file__).resolve().parents[1]


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=CHECKOUT, capture_output=True, text=True, check=True).stdout


def has_connection_files(root: Path) -> bool:
    """Whether ``root`` has the .env and database (or DATABASE_URL) the app's Gmail connection lives in."""
    try:
        env = (root / ".env").read_text(encoding="utf-8")
    except OSError:
        return False
    return bool(re.search(r"(?m)^\s*DATABASE_URL\s*=\s*\S", env)) or (root / "data" / "platform.db").is_file()


def main_checkout() -> Path | None:
    """The main worktree when this checkout is a linked one, like check_personal_data.personal_roots; None on any git failure."""
    try:
        common = Path(_git("rev-parse", "--path-format=absolute", "--git-common-dir").strip())
    except (OSError, subprocess.CalledProcessError):
        return None
    return common.parent if common.name == ".git" else None


def resolve_root(explicit: str | None) -> Path:
    """An explicit --root as given, never a fallback; else this checkout, else the main worktree when this one has no connection."""
    if explicit:
        return Path(explicit).expanduser().resolve()
    if has_connection_files(CHECKOUT):
        return CHECKOUT
    main = main_checkout()
    return main.resolve() if main is not None and main.resolve() != CHECKOUT.resolve() else CHECKOUT


def take_options(argv: list[str]) -> tuple[str | None, list[str], list[str]]:
    """The leading --root and --user, and everything after them untouched (a query may start with a minus)."""
    root, kept, index = None, [], 0
    while index < len(argv):
        token = argv[index]
        if token in ("--root", "--user") and index + 1 < len(argv):
            if token == "--root":
                root = argv[index + 1]
            else:
                kept += [token, argv[index + 1]]
            index += 2
        elif token.startswith("--root="):
            root, index = token.split("=", 1)[1], index + 1
        elif token.startswith("--user="):
            kept.append(token)
            index += 1
        else:
            break
    return root, kept, argv[index:]


def app_python(root: Path) -> Path | None:
    """The root's virtualenv Python, when it has one that is not the interpreter already running."""
    for candidate in (root / ".venv" / "Scripts" / "python.exe", root / ".venv" / "bin" / "python"):
        if candidate.is_file():
            try:
                if os.path.samefile(candidate, sys.executable):
                    return None
            except OSError:
                pass
            return candidate
    return None


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(errors="replace")  # a console that cannot show a path's characters must not hide the message
    explicit, options, rest = take_options(list(sys.argv[1:] if argv is None else argv))
    root = resolve_root(explicit)
    print(f"Pipeline checkout: {root}", file=sys.stderr)
    forwarded = ["--root", str(root), *options, *rest]
    python = app_python(root)
    if python is not None:
        env = {**os.environ, "PYTHONPATH": os.pathsep.join(filter(None, [str(CHECKOUT), os.environ.get("PYTHONPATH", "")]))}
        return subprocess.run([str(python), "-m", "opportunity_app.pipeline_mailbox", *forwarded], cwd=CHECKOUT, env=env).returncode
    sys.path.insert(0, str(CHECKOUT))
    try:
        from opportunity_app import pipeline_mailbox
    except ImportError:
        venv = "Scripts/python.exe" if os.name == "nt" else "bin/python"
        print(f"Run this with the app's Python: {root}/.venv/{venv} scripts/pipeline_mailbox.py (SETUP.md §1)", file=sys.stderr)
        return 2
    return pipeline_mailbox.main(forwarded)


if __name__ == "__main__":
    raise SystemExit(main())
