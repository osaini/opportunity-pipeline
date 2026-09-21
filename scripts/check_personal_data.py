"""Refuse to commit or push this student's personal data.

The repository is shared with other students, so identity, contact details,
and credentials belong only in gitignored files. This check reads those files
on the machine it runs on and blocks any commit or push whose content repeats
them. The personal values are never written into the repository; each copy
protects whoever set it up.

Sources of the blocklist, all optional and all gitignored:

- config/resume.json   the name and every contact field
- config/profile.json  the name
- .env                 every credential, token, account, and email address
- private/blocked-terms.txt  one extra term per line (employers, contacts,
                       anything else); lines starting with # are ignored
- the home directory path, such as C:\\Users\\<you>

In a linked worktree, the main checkout's personal files count too. It also refuses to add personal files by path even when forced past
.gitignore (git add -f), and databases or documents outside tests/.

Usage (the hooks in .githooks/ call the first two):

    python scripts/check_personal_data.py --staged
    python scripts/check_personal_data.py --push < <pre-push stdin>
    python scripts/check_personal_data.py --all    # every tracked file, plus history

Matches are reported by file, line, and kind, with the value masked. To commit
past a false positive, reword it, or run git with --no-verify once.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ZERO_SHA = "0" * 40
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# .env keys whose values are credentials or identify the student.
SECRET_KEY = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|ACCOUNT|EMAIL|CLIENT_ID|APP_ID|USER_AGENT|ATTACHMENT")
# Values that are settings rather than identity, even under a matching key.
NOT_PERSONAL = {"true", "false", "yes", "no", "on", "off", "none", "null"}
PERSONAL_PATHS = re.compile(
    r"""^(
        \.env(\..+)?$(?<!\.example)
      | config/resume\.json$
      | config/profile\.json$
      | config/[^/]*\.local\.json$
      | private/
      | output/
      | data/(?!manual_jobs\.csv$)
    )""",
    re.VERBOSE,
)
DOCUMENT_SUFFIXES = (".db", ".sqlite", ".sqlite3", ".pdf", ".docx", ".doc")
ADDED_LINE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


@dataclass(frozen=True)
class Needle:
    kind: str
    pattern: re.Pattern[str]


@dataclass(frozen=True)
class Hit:
    where: str
    kind: str
    sample: str

    def __str__(self) -> str:
        return f"  {self.where}: {self.kind} ({self.sample})"


# -- building the blocklist ---------------------------------------------------------
def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip().removeprefix("export ").strip()] = value.strip().strip("\"'")
    return values


def _literal(value: str, *, words: bool = False) -> re.Pattern[str]:
    body = r"\s+".join(re.escape(part) for part in value.split())
    return re.compile(rf"(?<![A-Za-z0-9]){body}(?![A-Za-z0-9])" if words else body, re.IGNORECASE)


def _phone(value: str) -> re.Pattern[str] | None:
    digits = re.sub(r"\D", "", value)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) < 7:
        return None
    # Any punctuation between the digits: (512) 555-0142, 512.555.0142, 5125550142.
    return re.compile(r"(?<!\d)" + r"[\s().+-]*".join(digits) + r"(?!\d)")


def _url(value: str) -> re.Pattern[str] | None:
    bare = re.sub(r"^(https?://)?(www\.)?", "", value.strip(), flags=re.IGNORECASE).rstrip("/")
    if "/" not in bare or len(bare) < 8:
        return None  # a bare domain, or too short to be specific to one person
    return re.compile(re.escape(bare) + r"(?![A-Za-z0-9_-])", re.IGNORECASE)


def build_needles(root: Path = ROOT, home: Path | None = None) -> list[Needle]:
    needles: list[Needle] = []
    seen: set[str] = set()

    def add(kind: str, pattern: re.Pattern[str] | None) -> None:
        if pattern is not None and pattern.pattern not in seen:
            seen.add(pattern.pattern)
            needles.append(Needle(kind, pattern))

    resume = _read_json(root / "config" / "resume.json")
    profile = _read_json(root / "config" / "profile.json")
    for source, data in (("resume", resume), ("profile", profile)):
        name = str(data.get("name") or "").strip()
        if len(name) >= 3:
            add(f"{source} name", _literal(name, words=True))
    contact = resume.get("contact") if isinstance(resume.get("contact"), dict) else {}
    for field, value in contact.items():
        value = str(value or "").strip()
        if not value:
            continue
        if field == "phone":
            add("resume phone", _phone(value))
        elif field == "email" or EMAIL.fullmatch(value):
            add(f"resume {field}", _literal(value))
        elif field == "location":
            continue  # a city is not specific to one person
        else:
            add(f"resume {field}", _url(value))

    for key, value in _read_env(root / ".env").items():
        for address in EMAIL.findall(value):
            add(f".env email ({key})", _literal(address))
        if SECRET_KEY.search(key) and len(value) >= 8 and value.lower() not in NOT_PERSONAL:
            add(f".env value ({key})", _literal(value))

    try:
        terms = (root / "private" / "blocked-terms.txt").read_text(encoding="utf-8").splitlines()
    except OSError:
        terms = []
    for term in terms:
        term = term.strip()
        if len(term) >= 3 and not term.startswith("#"):
            add("blocked term", _literal(term, words=True))

    home = home if home is not None else Path.home()
    if home.name and len(home.name) >= 3:
        # C:\Users\name, /Users/name, /home/name, and the JSON-escaped C:\\Users\\name.
        add("home directory", re.compile(rf"{re.escape(home.parent.name)}[\\/]+{re.escape(home.name)}(?![A-Za-z0-9_])", re.IGNORECASE))
    return needles


# -- scanning ---------------------------------------------------------------------
def _mask(text: str) -> str:
    text = text.strip()
    return text[:2] + "*" * max(len(text) - 4, 3) + text[-2:] if len(text) > 4 else "***"


def scan_text(text: str, where: str, needles: list[Needle]) -> list[Hit]:
    return [Hit(where, needle.kind, _mask(match.group(0))) for needle in needles for match in [needle.pattern.search(text)] if match]


def check_path(path: str) -> list[Hit]:
    path = path.replace("\\", "/")
    if PERSONAL_PATHS.match(path):
        return [Hit(path, "personal file", "gitignored for a reason; unstage it")]
    if path.lower().endswith(DOCUMENT_SUFFIXES) and not path.startswith("tests/"):
        return [Hit(path, "database or document", "keep real files out of the repository")]
    return []


def scan_diff(diff: str, needles: list[Needle], label: str = "") -> list[Hit]:
    """Scan only the added lines of a unified diff, reporting new-file line numbers."""
    hits: list[Hit] = []
    path, number = "", 0
    for line in diff.splitlines():
        if line.startswith("+++ "):
            path = line[4:].removeprefix("b/")
        elif line.startswith("--- ") or line.startswith("diff --git"):
            continue
        elif header := ADDED_LINE.match(line):
            number = int(header.group(1))
        elif line.startswith("+"):
            hits += scan_text(line[1:], f"{label}{path}:{number}", needles)
            number += 1
        elif not line.startswith("-"):
            number += 1
    return hits


def _git(*args: str) -> str:
    result = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, check=True)
    return result.stdout.decode("utf-8", errors="replace")


def check_staged(needles: list[Needle]) -> list[Hit]:
    added = _git("diff", "--cached", "--name-only", "--diff-filter=ACR", "-z").split("\0")
    hits = [hit for path in added if path for hit in check_path(path)]
    return hits + scan_diff(_git("diff", "--cached", "-U0", "--no-color", "--no-ext-diff", "--diff-filter=ACMR"), needles)


def commits_to_push(stdin: str) -> list[str]:
    commits: list[str] = []
    for line in stdin.splitlines():
        parts = line.split()
        if len(parts) != 4 or parts[1] == ZERO_SHA:
            continue  # malformed, or a branch deletion
        local, remote = parts[1], parts[3]
        exclude = ["--not", "--remotes"] if remote == ZERO_SHA else [f"^{remote}"]
        try:
            listed = _git("rev-list", local, *exclude)
        except subprocess.CalledProcessError:
            listed = _git("rev-list", local, "--not", "--remotes")  # remote sha not fetched here
        commits += [sha for sha in listed.split() if sha not in commits]
    return commits


def check_commits(commits: list[str], needles: list[Needle]) -> list[Hit]:
    hits: list[Hit] = []
    for sha in commits:
        short = sha[:8]
        hits += scan_text(_git("log", "-1", "--format=%B", sha), f"{short} commit message", needles)
        changed = _git("diff-tree", "--root", "-r", "--no-commit-id", "--name-only", "--diff-filter=ACR", "-z", sha)
        hits += [Hit(f"{short} {hit.where}", hit.kind, hit.sample) for path in changed.split("\0") if path for hit in check_path(path)]
        diff = _git("show", "--format=", "-U0", "--no-color", "--no-ext-diff", "--diff-filter=ACMR", sha)
        hits += scan_diff(diff, needles, label=f"{short} ")
    return hits


def check_tree(needles: list[Needle]) -> list[Hit]:
    hits: list[Hit] = []
    for path in _git("ls-files", "-z").split("\0"):
        if not path:
            continue
        hits += check_path(path)
        try:
            lines = (ROOT / path).read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        for number, line in enumerate(lines, start=1):
            hits += scan_text(line, f"{path}:{number}", needles)
    return hits


def personal_roots() -> list[Path]:
    """This checkout, plus the main checkout when this is a linked worktree.

    Personal files are gitignored, so a fresh worktree has none; they live in
    the checkout the student set up.
    """
    roots = [ROOT]
    try:
        common = Path(_git("rev-parse", "--path-format=absolute", "--git-common-dir").strip())
    except (OSError, subprocess.CalledProcessError):
        return roots
    main = common.parent if common.name == ".git" else None
    if main is not None and main.resolve() != ROOT.resolve():
        roots.append(main)
    return roots


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--staged", action="store_true", help="check what git commit is about to record")
    mode.add_argument("--push", action="store_true", help="check the commits a pre-push hook receives on stdin")
    mode.add_argument("--all", action="store_true", help="audit every tracked file and every commit")
    args = parser.parse_args(argv)

    needles = list({needle.pattern.pattern: needle for root in reversed(personal_roots()) for needle in build_needles(root)}.values())
    if args.staged:
        hits = check_staged(needles)
    elif args.push:
        hits = check_commits(commits_to_push(sys.stdin.read()), needles)
    else:
        hits = check_tree(needles) + check_commits(_git("rev-list", "--all").split(), needles)

    if not hits:
        if args.all:
            print(f"No personal data found ({len(needles)} personal values checked).")
        return 0
    print("Personal data check: refusing, because this would publish personal data:", file=sys.stderr)
    for hit in dict.fromkeys(hits):
        print(hit, file=sys.stderr)
    print(
        "Move the value into a gitignored file (config/profile.json, .env, private/), "
        "or reword it. Bypass once, only for a false positive, with --no-verify.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
