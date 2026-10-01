"""Loading the pipeline's configuration: the .env file, the profile and the layered source catalog."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from . import paths
from .env import iter_env_pairs
from .paths import display_path


def load_env_file(path: Path = paths.ENV_PATH) -> None:
    """Load `KEY=value` lines from a gitignored .env into the process environment.

    Credentials like USAJOBS_API_KEY otherwise have to be re-exported in every
    new shell, which is exactly the kind of setup step that gets skipped and
    then looks like a broken source. A real environment variable always wins,
    so `USAJOBS_API_KEY=... python3 pipeline.py fetch` still overrides the file.
    """
    for key, value in iter_env_pairs(path):
        if key and key not in os.environ:
            os.environ[key] = value


def load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"Missing {display_path(path)}. Restore it or run from the project root.")
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid JSON in {display_path(path)}: {exc}")


def load_profile(path: Path | None = None) -> dict[str, Any]:
    path = path or paths.PROFILE_PATH
    if not path.exists():
        raise SystemExit(
            f"Missing {display_path(path)}. Run `python -m opportunity_app.setup init` "
            "to create it from config/profile.example.json, then fill it in (SETUP.md)."
        )
    return load_json(path)


def _source_merge_key(source: dict[str, Any]) -> str:
    return f'{source.get("kind", "")}:{source_identity(source)}'.lower()


def merge_sources(base: dict[str, Any], local: dict[str, Any]) -> dict[str, Any]:
    """Layer one student's sources.local.json over the shared catalog.

    - Top-level keys in the local file replace the base value.
    - `ats_sources` concatenate; a local entry with the same kind and identity
      as a base entry replaces it, so a board can be retuned without editing
      the tracked catalog.
    - `include_base_catalog: false` drops every base ATS entry.
    - `enabled_sources` and `disabled_sources` list `kind:identity` keys or
      company names to switch on or off, e.g. a key-gated source once its key
      is in .env.
    - `agent_discovery` merges per channel; a local channel's keys win.
    - `manual_check_sources` concatenate, de-duplicated by name.
    """
    merged = dict(base)
    special = {
        "ats_sources",
        "include_base_catalog",
        "enabled_sources",
        "disabled_sources",
        "agent_discovery",
        "manual_check_sources",
    }
    for key, value in local.items():
        if key not in special and not key.startswith("_"):
            merged[key] = value

    base_ats = list(base.get("ats_sources", [])) if local.get("include_base_catalog", True) else []
    local_ats = list(local.get("ats_sources", []))
    local_keys = {_source_merge_key(source) for source in local_ats}
    ats = [source for source in base_ats if _source_merge_key(source) not in local_keys] + local_ats
    for switch, enabled in (("enabled_sources", True), ("disabled_sources", False)):
        names = {str(name).strip().lower() for name in local.get(switch, [])}
        if names:
            ats = [
                {**source, "enabled": enabled}
                if _source_merge_key(source) in names
                or str(source.get("company", "")).strip().lower() in names
                else source
                for source in ats
            ]
    merged["ats_sources"] = ats

    discovery = dict(base.get("agent_discovery", {}))
    for channel, settings in local.get("agent_discovery", {}).items():
        if isinstance(settings, dict) and isinstance(discovery.get(channel), dict):
            discovery[channel] = {**discovery[channel], **settings}
        else:
            discovery[channel] = settings
    merged["agent_discovery"] = discovery

    manual = list(base.get("manual_check_sources", []))
    names = {str(item.get("name", "")).lower() for item in manual}
    for item in local.get("manual_check_sources", []):
        if str(item.get("name", "")).lower() not in names:
            manual.append(item)
    merged["manual_check_sources"] = manual
    return merged


def load_sources(base_path: Path | None = None, local_path: Path | None = None) -> dict[str, Any]:
    """The shared catalog plus this student's overlay, when one exists."""
    base = load_json(base_path or paths.SOURCES_PATH)
    local_path = local_path or paths.SOURCES_LOCAL_PATH
    if not local_path.exists():
        return base
    return merge_sources(base, load_json(local_path))


def source_identity(source: dict[str, Any]) -> str:
    if source["kind"] == "workday":
        return f'{source["tenant"]}:{source["site"]}'
    return (
        # An explicit id keeps the source key stable for query-shaped sources
        # (usajobs, adzuna), whose keyword list can be retuned without
        # orphaning rows.
        source.get("id")
        or source.get("token")
        or source.get("site")
        or source.get("board")
        or source.get("company_id")
        or source.get("keyword")
        or "default"
    )


def source_key(source: dict[str, Any]) -> str:
    """The key a source's fetch runs and jobs are stored under: ``kind:identity``.

    Raises KeyError for a source with no ``kind``; `system_status.source_health`
    relies on that to skip a malformed entry. `_source_merge_key` is the same string
    lowercased, used only to layer sources.local.json over sources.json.
    """
    return f'{source["kind"]}:{source_identity(source)}'
