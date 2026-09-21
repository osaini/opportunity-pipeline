"""First-run setup, written to be driven by a student's coding agent (SETUP.md).

    python -m opportunity_app.setup init          create .env, profile, overlay, databases; turn on
                                                  the hooks that refuse to commit personal data
    python -m opportunity_app.setup status        what is configured, what is missing, what each key unlocks
    python -m opportunity_app.setup validate      check config/profile.json before scoring with it
    python -m opportunity_app.setup set-key NAME  store one secret in .env without echoing it

Every command is idempotent and safe to repeat. Nothing here overwrites a value
the student already set, prints a secret, or reaches the network. Add --json
for output an agent can parse.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import ROOT

MIN_PYTHON = (3, 11)
GENERATED_SECRETS = {
    "PIPELINE_WEB_TOKEN": lambda: secrets.token_urlsafe(32),
    "PIPELINE_EMPLOYER_TOKEN": lambda: secrets.token_urlsafe(32),
    "PIPELINE_ADMIN_TOKEN": lambda: secrets.token_urlsafe(32),
    # A Fernet key is 32 random bytes, urlsafe base64 encoded.
    "PIPELINE_CONNECTION_KEY": lambda: base64.urlsafe_b64encode(os.urandom(32)).decode(),
    "PIPELINE_WEBHOOK_SECRET": lambda: secrets.token_urlsafe(32),
}
KEY_NAME = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
EMPTY_OVERLAY = {
    "_note": (
        "Your own sources, layered on config/sources.json. See config/sources.local.example.json "
        "for every option. Only add a board that `pipeline.py discover-ats` confirmed."
    ),
    "enabled_sources": [],
    "disabled_sources": [],
    "ats_sources": [],
    "agent_discovery": {},
    "manual_check_sources": [],
}
# Optional integrations, what each unlocks, and where to get it. Nothing here is
# required: the pipeline runs on the public job boards with no keys at all.
INTEGRATIONS = [
    {
        "id": "usajobs",
        "env": ["USAJOBS_API_KEY", "USAJOBS_CONTACT_EMAIL"],
        "unlocks": "Every federal agency's student and intern postings in one source.",
        "get": "Free: https://developer.usajobs.gov/apirequest/ (use the same email for USAJOBS_CONTACT_EMAIL).",
        "then": 'Add "usajobs:federal-engineering" to enabled_sources in config/sources.local.json.',
    },
    {
        "id": "adzuna",
        "env": ["ADZUNA_APP_ID", "ADZUNA_APP_KEY"],
        "unlocks": "Radius search around your city, reaching small local employers with no modern job board.",
        "get": "Free: https://developer.adzuna.com/",
        "then": "Add an adzuna entry with your city to config/sources.local.json (see the example file).",
    },
    {
        "id": "claude-code",
        "cli": "claude",
        "unlocks": "The outreach deep search and AI drafting, on your own Claude subscription.",
        "get": "Install Claude Code and sign in once by running `claude`.",
    },
    {
        "id": "codex-cli",
        "cli": "codex",
        "unlocks": "The same, on a ChatGPT subscription.",
        "get": "Install Codex CLI and run `codex login`.",
    },
    {
        "id": "anthropic",
        "env": ["ANTHROPIC_API_KEY"],
        "unlocks": "The in-app career agent through the Anthropic API (pay per use).",
        "get": "https://console.anthropic.com/",
    },
    {
        "id": "openai",
        "env": ["OPENAI_API_KEY"],
        "unlocks": "The in-app career agent through the OpenAI API (pay per use).",
        "get": "https://platform.openai.com/",
    },
    {
        "id": "jev",
        "env": ["TYPESAFE_API_KEY"],
        "unlocks": "An optional second-opinion panel on a posting. Never changes scores; skip it if you have no access.",
        "get": "TypeSafe (waitlisted): https://typesafe.ai/",
    },
    {
        "id": "gmail-drafts",
        "env": ["GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET"],
        "unlocks": "Approved outreach emails open as Gmail drafts with your resume attached. Without it they open as mailto links.",
        "get": "Your own Google Cloud OAuth client; follow README.md, section 'Gmail drafts'.",
    },
    {
        "id": "sec-edgar",
        "env": ["PIPELINE_SEC_USER_AGENT"],
        "unlocks": "Company locations from SEC Form D filings for outreach targets.",
        "get": 'No signup: set it to "Your Name you@example.com".',
    },
]


@dataclass(frozen=True)
class Paths:
    root: Path

    @property
    def env(self) -> Path:
        return self.root / ".env"

    @property
    def env_example(self) -> Path:
        return self.root / ".env.example"

    @property
    def profile(self) -> Path:
        return self.root / "config" / "profile.json"

    @property
    def profile_example(self) -> Path:
        return self.root / "config" / "profile.example.json"

    @property
    def overlay(self) -> Path:
        return self.root / "config" / "sources.local.json"

    @property
    def legacy_db(self) -> Path:
        return self.root / "data" / "pipeline.db"

    @property
    def platform_db(self) -> Path:
        return self.root / "data" / "platform.db"


# -- .env ------------------------------------------------------------------------


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def set_env_values(path: Path, updates: dict[str, str], *, overwrite: bool = False) -> list[str]:
    """Write KEY=value lines in place, keeping comments and order; return the keys changed."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    current = read_env(path)
    changed: list[str] = []
    remaining = dict(updates)
    for index, line in enumerate(lines):
        key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else ""
        if key in remaining:
            value = remaining.pop(key)
            if overwrite or not current.get(key):
                lines[index] = f"{key}={value}"
                changed.append(key)
    for key, value in remaining.items():
        lines.append(f"{key}={value}")
        changed.append(key)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    temporary.replace(path)
    if os.name == "posix":
        path.chmod(0o600)
    return changed


# -- commands --------------------------------------------------------------------


def detect_agent_cli() -> str:
    for provider, binary, override in (
        ("claude-code", "claude", "PIPELINE_CLAUDE_BIN"),
        ("codex-cli", "codex", "PIPELINE_CODEX_BIN"),
    ):
        if shutil.which(os.environ.get(override) or binary):
            return provider
    return ""


def init(paths: Paths, *, migrate: bool = True) -> dict[str, Any]:
    report: dict[str, Any] = {"created": [], "kept": [], "generated_secrets": [], "warnings": []}
    if sys.version_info < MIN_PYTHON:
        report["warnings"].append(
            f"Python {sys.version_info.major}.{sys.version_info.minor} is older than the supported "
            f"{MIN_PYTHON[0]}.{MIN_PYTHON[1]}; install a newer Python."
        )

    if paths.env.exists():
        report["kept"].append(".env")
    else:
        shutil.copyfile(paths.env_example, paths.env)
        report["created"].append(".env")
    existing = read_env(paths.env)
    generated = {name: make() for name, make in GENERATED_SECRETS.items() if not existing.get(name)}
    agent = detect_agent_cli()
    extras = {}
    if agent and not existing.get("PIPELINE_OUTREACH_DISCOVERY_PROVIDER"):
        extras["PIPELINE_OUTREACH_DISCOVERY_PROVIDER"] = agent
    if generated or extras:
        set_env_values(paths.env, {**generated, **extras})
    elif os.name == "posix":
        paths.env.chmod(0o600)
    report["generated_secrets"] = sorted(generated)
    report["agent_cli"] = agent or None

    if paths.profile.exists():
        report["kept"].append("config/profile.json")
    else:
        shutil.copyfile(paths.profile_example, paths.profile)
        report["created"].append("config/profile.json")
    if paths.overlay.exists():
        report["kept"].append("config/sources.local.json")
    else:
        paths.overlay.write_text(json.dumps(EMPTY_OVERLAY, indent=2) + "\n", encoding="utf-8")
        report["created"].append("config/sources.local.json")

    if migrate:
        created_db = not paths.platform_db.exists()
        _ensure_databases(paths)
        (report["created"] if created_db else report["kept"]).append("data/platform.db")
    hooks = enable_personal_data_hooks(paths.root)
    if hooks:
        report["personal_data_hooks"] = hooks
        if hooks.startswith("not enabled"):
            report["warnings"].append(f"Personal data hooks {hooks}")
    return report


def enable_personal_data_hooks(root: Path) -> str:
    """Point git at .githooks/, whose hooks refuse to commit or push personal data.

    Returns "" when this is not a git checkout that ships the hooks.
    """
    if not (root / ".githooks").is_dir() or not (root / ".git").exists():
        return ""

    def git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)

    try:
        current = git("config", "--get", "core.hooksPath").stdout.strip()
        if current == ".githooks":
            return "enabled"
        if current:
            return f"not enabled: core.hooksPath is already {current!r}; run scripts/check_personal_data.py from it"
        return "enabled" if git("config", "core.hooksPath", ".githooks").returncode == 0 else "not enabled: git config failed"
    except OSError:
        return "not enabled: git is not installed"


def _ensure_databases(paths: Paths) -> None:
    import sqlite3

    import pipeline

    from .schema import migrate_legacy_database

    paths.legacy_db.parent.mkdir(parents=True, exist_ok=True)
    original = pipeline.DB_PATH
    pipeline.DB_PATH = paths.legacy_db
    try:
        conn: sqlite3.Connection = pipeline.connect()
        conn.close()
    finally:
        pipeline.DB_PATH = original
    migrate_legacy_database(paths.legacy_db, paths.platform_db, paths.profile)


REGION_FIELDS = {"name": str, "state_markers": list, "places": list}
PROFILE_TYPES: dict[str, tuple[type, ...]] = {
    "name": (str,),
    "school": (str,),
    "degree": (str,),
    "graduation_year": (int, type(None)),
    "degree_keywords": (list,),
    "preferred_role_types": (list,),
    "preferred_locations": (list,),
    "regions": (list,),
    "out_of_region_penalty": (int,),
    "remote_ok": (bool,),
    "willing_to_relocate": (bool, type(None)),
    "skills": (list,),
    "interest_keywords": (list,),
    "deprioritize_title_keywords": (list,),
    "max_years_experience": (int,),
    "work_authorized_us": (bool, type(None)),
    "us_citizen": (bool, type(None)),
    "requires_sponsorship": (bool, type(None)),
    "hours_per_week": (int, type(None)),
    "available_terms": (list,),
}
ROLE_TYPES = {"internship", "externship", "co-op", "research", "part_time", "early_career", "full_time", "other"}
TERM = re.compile(r"^(spring|summer|fall|winter) 20\d{2}$")


def validate_profile(profile: Any) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(profile, dict):
        return {"ok": False, "errors": ["config/profile.json must hold a JSON object"], "warnings": [], "missing": []}
    for field, types in PROFILE_TYPES.items():
        if field in profile and not isinstance(profile[field], types):
            # bool is an int subclass; reject it where a number is expected.
            errors.append(f"{field} should be {' or '.join(t.__name__ for t in types)}")
        elif field in profile and int in types and isinstance(profile[field], bool):
            errors.append(f"{field} should be a number, not true/false")
    for index, region in enumerate(profile.get("regions") or []):
        if not isinstance(region, dict):
            errors.append(f"regions[{index}] should be an object")
            continue
        for key, kind in REGION_FIELDS.items():
            if not isinstance(region.get(key), kind) or not region.get(key):
                errors.append(f"regions[{index}].{key} is required ({kind.__name__})")
        if region.get("places") and not region.get("state_markers"):
            warnings.append(f"regions[{index}] places only match alongside a state marker")
    for role in profile.get("preferred_role_types") or []:
        if role not in ROLE_TYPES:
            warnings.append(f"preferred_role_types has unknown value {role!r}; known: {', '.join(sorted(ROLE_TYPES))}")
    for term in profile.get("available_terms") or []:
        if not isinstance(term, str) or not TERM.match(term.lower()):
            errors.append(f"available_terms entry {term!r} should look like 'summer 2027'")
    if profile.get("requires_sponsorship") is True and profile.get("work_authorized_us") is True:
        warnings.append("requires_sponsorship and work_authorized_us are both true; confirm with the student")
    from .profile import COMPLETENESS_FIELDS, is_answered

    missing = [field for field in COMPLETENESS_FIELDS if not is_answered(profile.get(field))]
    return {"ok": not errors, "errors": errors, "warnings": warnings, "missing": missing}


def status(paths: Paths) -> dict[str, Any]:
    env = {**read_env(paths.env), **{key: value for key, value in os.environ.items() if key in _ENV_NAMES}}
    integrations = []
    for item in INTEGRATIONS:
        if "cli" in item:
            configured = bool(shutil.which(item["cli"]))
        else:
            configured = all(env.get(name) for name in item["env"])
        entry = {key: item[key] for key in ("id", "unlocks", "get") if key in item}
        entry["configured"] = configured
        if configured and item.get("then"):
            entry["then"] = item["then"]
        integrations.append(entry)

    profile_report: dict[str, Any] = {"exists": paths.profile.exists()}
    if paths.profile.exists():
        try:
            profile_report.update(validate_profile(json.loads(paths.profile.read_text(encoding="utf-8"))))
        except ValueError as exc:
            profile_report.update(ok=False, errors=[f"invalid JSON: {exc}"])

    sources_report: dict[str, Any] = {"overlay_exists": paths.overlay.exists()}
    try:
        from pipeline import load_sources

        merged = load_sources(paths.root / "config" / "sources.json", paths.overlay)
        enabled = [source for source in merged["ats_sources"] if source.get("enabled", True)]
        sources_report["enabled"] = len(enabled)
        sources_report["needs_keys"] = sorted(
            f'{source["kind"]}:{source.get("id", "")}'
            for source in enabled
            if (source["kind"] == "usajobs" and not env.get("USAJOBS_API_KEY"))
            or (source["kind"] == "adzuna" and not (env.get("ADZUNA_APP_ID") and env.get("ADZUNA_APP_KEY")))
        )
    except (SystemExit, OSError, ValueError, KeyError) as exc:
        sources_report["error"] = str(exc)

    return {
        "python": ".".join(map(str, sys.version_info[:3])),
        "python_ok": sys.version_info >= MIN_PYTHON,
        "env_exists": paths.env.exists(),
        "sign_in_token": bool(env.get("PIPELINE_WEB_TOKEN")),
        "database": paths.platform_db.exists(),
        "profile": profile_report,
        "sources": sources_report,
        "integrations": integrations,
        "next": _next_steps(paths, env, profile_report),
    }


_ENV_NAMES = {name for item in INTEGRATIONS for name in item.get("env", [])} | set(GENERATED_SECRETS)


def _next_steps(paths: Paths, env: dict[str, str], profile: dict[str, Any]) -> list[str]:
    steps = []
    if not paths.env.exists() or not env.get("PIPELINE_WEB_TOKEN") or not paths.platform_db.exists():
        steps.append("python -m opportunity_app.setup init")
    if profile.get("errors"):
        steps.append("Fix config/profile.json: " + "; ".join(profile["errors"]))
    elif profile.get("missing"):
        steps.append("Interview the student for: " + ", ".join(profile["missing"]))
    if not steps:
        steps.append("python pipeline.py run, then python -m opportunity_app.launch open")
    return steps


def set_key(paths: Paths, name: str, value: str | None = None) -> str:
    if not KEY_NAME.match(name):
        raise SystemExit(f"{name!r} is not an environment variable name")
    if value is None:
        value = getpass.getpass(f"{name} (input hidden): ") if sys.stdin.isatty() else sys.stdin.readline()
    value = value.strip()
    if not value:
        raise SystemExit("No value given; nothing changed.")
    if any(character in value for character in "\r\n"):
        raise SystemExit("The value must be one line.")
    if not paths.env.exists():
        shutil.copyfile(paths.env_example, paths.env)
    set_env_values(paths.env, {name: value}, overwrite=True)
    return f"Saved {name} to .env."


def _print(report: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(report, indent=2))
        return
    if isinstance(report, str):
        print(report)
        return
    for key, value in report.items():
        print(f"{key}: {json.dumps(value) if not isinstance(value, str) else value}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--json", action="store_true", help="Machine-readable output")
    sub = parser.add_subparsers(dest="command", required=True)
    init_parser = sub.add_parser("init", help="Create .env, profile, source overlay, and databases")
    init_parser.add_argument("--no-migrate", action="store_true", help="Skip creating the databases")
    sub.add_parser("status", help="Report configuration and optional integrations")
    sub.add_parser("validate", help="Check config/profile.json")
    key_parser = sub.add_parser("set-key", help="Store one secret in .env (reads it hidden, or from stdin)")
    key_parser.add_argument("name")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    paths = Paths(ROOT)
    if args.command == "init":
        _print(init(paths, migrate=not args.no_migrate), args.json)
        return 0
    if args.command == "status":
        _print(status(paths), args.json)
        return 0
    if args.command == "validate":
        if not paths.profile.exists():
            raise SystemExit("config/profile.json does not exist; run `python -m opportunity_app.setup init`.")
        try:
            report = validate_profile(json.loads(paths.profile.read_text(encoding="utf-8")))
        except ValueError as exc:
            report = {"ok": False, "errors": [f"invalid JSON: {exc}"], "warnings": [], "missing": []}
        _print(report, args.json)
        return 0 if report["ok"] else 1
    _print(set_key(paths, args.name), args.json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
