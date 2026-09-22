"""Early programs: a hand-researched list the student tracks in its own tab.

Most internship postings quietly assume a particular class year. The programs
that genuinely take a student at *their* stage are scattered across
companies, labs, and universities, and many are not job postings at all
(scholarships, externships, research programs). They do not fit the
opportunity inventory, which is refreshed from sources and purged on expiry,
so each student researches their own list, during setup (SETUP.md), into a
private file, ``config/early_programs.local.json``:

    {
      "checked_on": "2026-09-21",
      "label": "First-year",
      "audience": "first-years",
      "programs": [
        {
          "id": "spacex-summer-2027",
          "name": "Summer 2027 Engineering Internship/Co-op",
          "host": "SpaceX",
          "url": "https://...",
          "evidence": "explicit",
          "kind": "Paid internship",
          "sector": "Aerospace and defense",
          "eligibility": "...",
          "pay": "...",
          "opens_on": null,
          "deadline_on": "2026-12-13",
          "deadline_note": "Rolling; apply early",
          "source_note": "Official posting",
          "closed_note": "",
          "notes": ""
        }
      ]
    }

``label`` names the tab and ``audience`` names the students the list is for
(both optional; nothing about any one student is built in). ``evidence``
records how strongly the host says that audience may apply, so a weak claim
is never shown as a confirmed one: ``explicit`` (the host names that class
year), ``not_named`` (no class-year limit is stated), or ``unverified`` (the
official page could not be checked). Dates are the ones the host published;
nothing here estimates one. The only thing stored in the database is what the
student did about each entry.
"""

from __future__ import annotations

import json
import re
import sqlite3
import urllib.parse
from datetime import date, datetime
from pathlib import Path
from typing import Any

from . import ROOT
from .schema import LOCAL_USER_ID, utc_now
from .user_time import user_timezone

DEFAULT_EARLY_PROGRAMS = ROOT / "config" / "early_programs.local.json"

EVIDENCE_LEVELS = ("explicit", "not_named", "unverified")
DEFAULT_LABEL = "Programs"
LABEL_LIMIT = 24
AUDIENCE_LIMIT = 40
STATUSES = ("todo", "applied", "skipped")
BUCKETS = ("open", "upcoming", "done", "closed")
TEXT_FIELDS = ("kind", "sector", "eligibility", "pay", "deadline_note", "source_note", "closed_note", "notes")
TEXT_LIMIT = 600
MAX_PROGRAMS = 500

_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,79}")
_DATE_ONLY = re.compile(r"\d{4}-\d{2}-\d{2}")


class EarlyProgramNotFoundError(LookupError):
    pass


def evidence_labels(audience: str) -> dict[str, str]:
    """How each evidence level reads for this student's audience."""
    if audience:
        return {
            "explicit": f"Names {audience}",
            "not_named": "No class-year limit stated",
            "unverified": f"Eligibility for {audience} unverified",
        }
    return {
        "explicit": "Names your class year",
        "not_named": "No class-year limit stated",
        "unverified": "Class-year eligibility unverified",
    }


def _text(value: Any, limit: int = TEXT_LIMIT) -> str:
    return str(value or "").strip()[:limit] if isinstance(value, (str, int, float)) else ""


def _date(value: Any) -> str | None:
    """A published ``YYYY-MM-DD`` date, or None; anything else is invalid."""
    if value in (None, ""):
        return None
    if not isinstance(value, str) or not _DATE_ONLY.fullmatch(value):
        raise ValueError("dates must be YYYY-MM-DD")
    return date.fromisoformat(value).isoformat()


def _url(value: Any) -> str:
    parsed = urllib.parse.urlsplit(str(value or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("url must be a public http(s) address")
    return urllib.parse.urlunsplit(parsed)


def _clean_program(raw: Any, labels: dict[str, str]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("each program must be an object")
    program_id = str(raw.get("id") or "")
    if not _ID.fullmatch(program_id):
        raise ValueError("id must be lowercase letters, digits, and hyphens")
    name, host = _text(raw.get("name"), 200), _text(raw.get("host"), 200)
    if not name or not host:
        raise ValueError("name and host are required")
    evidence = str(raw.get("evidence") or "")
    if evidence not in EVIDENCE_LEVELS:
        raise ValueError(f"evidence must be one of {', '.join(EVIDENCE_LEVELS)}")
    program = {
        "id": program_id,
        "name": name,
        "host": host,
        "url": _url(raw.get("url")),
        "evidence": evidence,
        "evidence_label": labels[evidence],
        "opens_on": _date(raw.get("opens_on")),
        "deadline_on": _date(raw.get("deadline_on")),
    }
    program.update({field: _text(raw.get(field)) for field in TEXT_FIELDS})
    return program


def load_programs(path: Path | None) -> dict[str, Any]:
    """Read and validate the list. Invalid entries are skipped and counted."""
    result: dict[str, Any] = {
        "configured": False, "checked_on": None, "label": DEFAULT_LABEL, "audience": "",
        "evidence_labels": evidence_labels(""), "programs": [], "skipped": 0, "problems": [], "error": "",
    }
    if path is None or not path.is_file():
        return result
    result["configured"] = True
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        result["error"] = f"{path.name} could not be read: {exc.__class__.__name__}"
        return result
    raw_programs = document.get("programs") if isinstance(document, dict) else None
    if not isinstance(raw_programs, list):
        result["error"] = f'{path.name} needs a "programs" list'
        return result
    try:
        result["checked_on"] = _date(document.get("checked_on"))
    except ValueError:
        result["checked_on"] = None
    result["label"] = _text(document.get("label"), LABEL_LIMIT) or DEFAULT_LABEL
    result["audience"] = _text(document.get("audience"), AUDIENCE_LIMIT)
    labels = result["evidence_labels"] = evidence_labels(result["audience"])
    seen: set[str] = set()
    for index, raw in enumerate(raw_programs[:MAX_PROGRAMS]):
        entry = str(raw.get("id") or f"#{index + 1}") if isinstance(raw, dict) else f"#{index + 1}"
        try:
            program = _clean_program(raw, labels)
        except ValueError as exc:
            result["skipped"] += 1
            result["problems"].append({"entry": entry, "error": str(exc)})
            continue
        if program["id"] in seen:
            result["skipped"] += 1
            result["problems"].append({"entry": entry, "error": "duplicate id"})
            continue
        seen.add(program["id"])
        result["programs"].append(program)
    result["skipped"] += max(0, len(raw_programs) - MAX_PROGRAMS)
    return result


def _statuses(conn: sqlite3.Connection, user_id: str) -> dict[str, dict[str, str]]:
    rows = conn.execute(
        "SELECT program_id, status, updated_at FROM early_program_status WHERE user_id=?",
        (user_id,),
    ).fetchall()
    return {str(row["program_id"]): {"status": str(row["status"]), "updated_at": str(row["updated_at"])} for row in rows}


def _bucket(program: dict[str, Any], status: str, today: date) -> str:
    if status != "todo":
        return "done"
    deadline = program["deadline_on"]
    if program["closed_note"] or (deadline and date.fromisoformat(deadline) < today):
        return "closed"
    if program["opens_on"] and date.fromisoformat(program["opens_on"]) > today:
        return "upcoming"
    return "open"


def _sort_key(program: dict[str, Any]) -> tuple[Any, ...]:
    # Soonest deadline first; undated entries follow, ordered by opening date.
    return (
        program["deadline_on"] is None,
        program["deadline_on"] or "",
        program["opens_on"] or "9999-12-31",
        program["host"].casefold(),
        program["id"],
    )


def programs_path_for(path: Path | None, user_id: str) -> Path | None:
    """The list file this user may see: the owner's private file is the owner's alone.

    ``early_programs.local.json`` is researched for the local owner's stage, so
    any other account on the same install gets the honest unconfigured state
    rather than someone else's list.
    """
    return path if user_id == LOCAL_USER_ID else None


def early_programs(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    path: Path | None,
    now: datetime | None = None,
) -> dict[str, Any]:
    path = programs_path_for(path, user_id)
    loaded = load_programs(path)
    today = user_timezone(conn, user_id).today(now)
    statuses = _statuses(conn, user_id)
    items = []
    for program in sorted(loaded["programs"], key=_sort_key):
        record = statuses.get(program["id"], {})
        status = record.get("status", "todo")
        bucket = _bucket(program, status, today)
        deadline = program["deadline_on"]
        items.append({
            **program,
            "status": status,
            "status_updated_at": record.get("updated_at"),
            "bucket": bucket,
            "days_left": (date.fromisoformat(deadline) - today).days if deadline else None,
        })
    counts = {bucket: sum(1 for item in items if item["bucket"] == bucket) for bucket in BUCKETS}
    return {
        "configured": loaded["configured"],
        "file": path.name if path else "",
        "checked_on": loaded["checked_on"],
        "label": loaded["label"],
        "audience": loaded["audience"],
        "evidence_labels": loaded["evidence_labels"],
        "error": loaded["error"],
        "skipped": loaded["skipped"],
        "today": today.isoformat(),
        "counts": counts,
        "total": len(items),
        "items": items,
    }


def set_program_status(
    conn: sqlite3.Connection,
    program_id: str,
    *,
    user_id: str,
    status: str,
    path: Path | None,
) -> dict[str, Any]:
    if status not in STATUSES:
        raise ValueError(f"status must be one of {', '.join(STATUSES)}")
    path = programs_path_for(path, user_id)
    if not any(program["id"] == program_id for program in load_programs(path)["programs"]):
        raise EarlyProgramNotFoundError(program_id)
    timestamp = utc_now()
    with conn:
        if status == "todo":
            conn.execute(
                "DELETE FROM early_program_status WHERE user_id=? AND program_id=?",
                (user_id, program_id),
            )
        else:
            conn.execute(
                """
                INSERT INTO early_program_status(user_id, program_id, status, updated_at)
                VALUES(?, ?, ?, ?)
                ON CONFLICT(user_id, program_id) DO UPDATE SET
                    status = excluded.status,
                    updated_at = excluded.updated_at
                """,
                (user_id, program_id, status, timestamp),
            )
    return {"id": program_id, "status": status, "status_updated_at": None if status == "todo" else timestamp}
