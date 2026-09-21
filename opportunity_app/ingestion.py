"""Scheduled ingestion: pipeline CLI stages and the RSS discovery fetcher.

The legacy CLI stays authoritative for ingestion. The worker invokes it via
subprocess so a hung fetch can never take the web worker down, records every
attempt in ``ingestion_runs``, and honors PIPELINE_DB so tests run hermetically.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from pipeline import load_sources

from .schema import utc_now

PIPELINE_CLI = Path(__file__).resolve().parents[1] / "pipeline.py"
DEFAULT_DISCOVERED_PATH = Path(__file__).resolve().parents[1] / "data" / "discovered_jobs.json"
SOURCES_CONFIG = Path(__file__).resolve().parents[1] / "config" / "sources.json"

STAGE_COMMANDS = {
    "fetch": ["fetch"],
    "enrich": ["enrich"],
    "score": ["score"],
    "liveness": [],
    "report": ["report"],
    # Not a scheduled worker stage (see worker.STAGES); rss_discovery_handler
    # runs it after writing fetched feed entries to the discovered-jobs file.
    "import-discovered": ["import-discovered"],
}


def run_pipeline_stage(stage: str, *, db_path: Path | str | None = None, extra_args: list[str] | None = None, timeout: int = 900) -> dict[str, Any]:
    if stage not in STAGE_COMMANDS:
        raise ValueError(f"Unsupported pipeline stage: {stage}")
    command = [sys.executable, str(PIPELINE_CLI), *STAGE_COMMANDS[stage], *(extra_args or [])]
    env = dict(os.environ)
    env.pop("PIPELINE_NOTIFICATIONS_LIVE", None)
    if db_path is not None:
        env["PIPELINE_DB"] = str(db_path)
    started = utc_now()
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(PIPELINE_CLI.parent),
            env=env,
        )
        return {
            "stage": stage,
            "status": "success" if completed.returncode == 0 else "failed",
            "started_at": started,
            "finished_at": utc_now(),
            "exit_code": completed.returncode,
            "stdout_tail": completed.stdout[-2000:],
            "stderr_tail": completed.stderr[-2000:],
        }
    except subprocess.TimeoutExpired:
        return {
            "stage": stage,
            "status": "failed",
            "started_at": started,
            "finished_at": utc_now(),
            "exit_code": None,
            "stdout_tail": "",
            "stderr_tail": f"stage timed out after {timeout}s",
        }


def record_ingestion_run(conn: sqlite3.Connection, result: dict[str, Any]) -> int:
    with conn:
        cursor = conn.execute(
            "INSERT INTO ingestion_runs(stage, status, started_at, finished_at, detail_json) VALUES(?, ?, ?, ?, ?)",
            (result["stage"], result["status"], result["started_at"], result["finished_at"], json.dumps({
                "exit_code": result.get("exit_code"),
                "stdout_tail": result.get("stdout_tail", ""),
                "stderr_tail": result.get("stderr_tail", ""),
            })),
        )
    return int(cursor.lastrowid)


def make_stage_handler(conn: sqlite3.Connection, *, db_path: Path | str | None = None):
    def handler(payload: dict[str, Any]) -> dict[str, Any]:
        stage = str(payload.get("stage", ""))
        target = payload.get("db_path") or db_path
        result = run_pipeline_stage(stage, db_path=target, extra_args=list(payload.get("extra_args") or []))
        record_ingestion_run(conn, result)
        if result["status"] != "success":
            raise RuntimeError(f"pipeline stage {stage} failed: {result['stderr_tail'][:500]}")
        return result

    return handler


def _entry_text(entry: ElementTree.Element, *paths: str, namespaces: dict[str, str] | None = None) -> str:
    for path in paths:
        node = entry.find(path, namespaces)
        if node is not None and (node.text or "").strip():
            return " ".join(node.text.split())
        # Atom <link href="..."> carries the URL in an attribute.
        href = node.get("href") if node is not None else None
        if href:
            return href
    return ""


def _parse_posted(raw: str) -> str:
    raw = raw.strip()
    if not raw:
        return ""
    try:
        return parsedate_to_datetime(raw).astimezone(timezone.utc).isoformat()
    except (TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).isoformat()
    except ValueError:
        return ""


def parse_feed(xml_text: str, *, channel_company: str = "") -> list[dict[str, Any]]:
    """Normalize RSS 2.0 or Atom entries into the discovered-jobs shape."""
    root = ElementTree.fromstring(xml_text)
    channel_title = ""
    entries: list[ElementTree.Element] = []
    if root.tag.rsplit("}", 1)[-1] == "feed":
        channel_title = _entry_text(root, "{http://www.w3.org/2005/Atom}title")
        entries = root.findall("{http://www.w3.org/2005/Atom}entry")
        namespace = {"atom": "http://www.w3.org/2005/Atom"}
        items = []
        for entry in entries:
            items.append({
                "title": _entry_text(entry, "atom:title", namespaces=namespace),
                "url": _entry_text(entry, "atom:link", namespaces=namespace),
                "description": _entry_text(entry, "atom:summary", namespaces=namespace),
                "posted_raw": _entry_text(entry, "atom:updated", namespaces=namespace),
            })
    else:
        channel_node = root.find("channel")
        if channel_node is not None:
            channel_title = _entry_text(channel_node, "title")
            entries = channel_node.findall("item")
        items = [
            {
                "title": _entry_text(entry, "title"),
                "url": _entry_text(entry, "link"),
                "description": _entry_text(entry, "description"),
                "posted_raw": _entry_text(entry, "pubDate"),
            }
            for entry in entries
        ]
    company = channel_company.strip() or channel_title or "Unknown Feed"
    results = []
    for item in items:
        if not item["title"] or not item["url"]:
            continue
        record = {
            "channel": "rss",
            "title": item["title"][:300],
            "company": company[:200],
            "location": "",
            "url": item["url"],
        }
        posted = _parse_posted(item["posted_raw"])
        if posted:
            record["posted_at"] = posted
        if item["description"]:
            record["description"] = item["description"][:4000]
        results.append(record)
    return results


def fetch_rss_discoveries(feeds: list[dict[str, Any] | str], *, timeout: int = 20) -> tuple[list[dict[str, Any]], list[str]]:
    """Fetch configured feeds; returns (discoveries, feed_errors)."""
    discoveries: list[dict[str, Any]] = []
    errors: list[str] = []
    for feed in feeds:
        if isinstance(feed, str):
            feed = {"url": feed}
        url = str(feed.get("url", "")).strip()
        if not url:
            continue
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "opportunity-pipeline-rss/1.0"})
            with urllib.request.urlopen(request, timeout=timeout) as response:
                xml_text = response.read().decode("utf-8", errors="replace")
            discoveries.extend(parse_feed(xml_text, channel_company=str(feed.get("company", ""))))
        except (OSError, ElementTree.ParseError) as exc:
            errors.append(f"{url}: {exc}")
    return discoveries, errors


def rss_discovery_handler(
    conn: sqlite3.Connection,
    *,
    discovered_path: Path | str | None = None,
    db_path: Path | str | None = None,
) -> dict[str, Any]:
    """Fetch RSS feeds from config, write the discovered JSON, and import it."""
    config = load_sources(SOURCES_CONFIG, SOURCES_CONFIG.with_name("sources.local.json"))
    feeds = list(config.get("agent_discovery", {}).get("rss", {}).get("feeds", []))
    if not feeds:
        return {"fetched": 0, "imported_run": None, "skipped": "no RSS feeds configured"}
    discoveries, errors = fetch_rss_discoveries(feeds)
    target = Path(discovered_path or DEFAULT_DISCOVERED_PATH)
    existing: list[dict[str, Any]] = []
    if target.exists():
        try:
            existing = json.loads(target.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            existing = []
    seen_urls = {str(item.get("url")) for item in existing}
    fresh = [item for item in discoveries if item["url"] not in seen_urls]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(existing + fresh, indent=2), encoding="utf-8")
    result = run_pipeline_stage("import-discovered", db_path=db_path, extra_args=[str(target)])
    record_ingestion_run(conn, {**result, "stage": "rss_import"})
    if errors:
        raise RuntimeError("rss fetch errors: " + "; ".join(errors))
    if result["status"] != "success":
        raise RuntimeError(f"import-discovered failed: {result['stderr_tail'][:500]}")
    return {"fetched": len(discoveries), "new": len(fresh), "imported_run": True}
