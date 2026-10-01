"""Reports: the shortlist files, the dashboard, status changes and the doctor check."""

from __future__ import annotations

import csv
import html
import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any

from . import paths
from .clock import now_iso, parse_datetime
from .identity import sort_key
from .read_model import RANKED_VIEW_PER_COMPANY
from .regions import region_label
from .store import VALID_STATUSES


def stale_label(last_seen_at: str, stale_after_days: int) -> str:
    last_seen = parse_datetime(last_seen_at)
    if not last_seen:
        return "unknown"
    age = (datetime.now(timezone.utc) - last_seen.astimezone(timezone.utc)).days
    return f"{age}d since checked" + (" — STALE" if age > stale_after_days else "")


def display_reasons(reasons: list[str], limit: int = 5) -> list[str]:
    return [reason for reason in reasons if reason != "35 base"][:limit]


def cap_per_company(
    ranked: list[sqlite3.Row], limit: int, per_company: int = RANKED_VIEW_PER_COMPANY
) -> tuple[list[sqlite3.Row], dict[str, int]]:
    """The first `limit` of `ranked`, keeping each employer's top `per_company`.

    Also returns, per employer that reached the cap, how many of its postings
    were left out, keyed by `sort_key`. An employer cut short by `limit`
    rather than the cap is not in it: those postings did not rank high enough,
    which the shortlist's length already says.
    """

    totals: dict[str, int] = {}
    for job in ranked:
        key = sort_key(job["company"])
        totals[key] = totals.get(key, 0) + 1
    shown: dict[str, int] = {}
    kept: list[sqlite3.Row] = []
    for job in ranked:
        if len(kept) >= limit:
            break
        key = sort_key(job["company"])
        if shown.get(key, 0) >= per_company:
            continue
        shown[key] = shown.get(key, 0) + 1
        kept.append(job)
    hidden = {
        key: totals[key] - count
        for key, count in shown.items()
        if count >= per_company and totals[key] > count
    }
    return kept, hidden


def report(conn: sqlite3.Connection, sources_config: dict[str, Any], limit: int) -> int:
    stale_days = int(sources_config.get("stale_after_days", 7))
    ranked = conn.execute(
        """
        SELECT * FROM jobs
        WHERE active=1 AND duplicate_of IS NULL AND status NOT IN ('rejected', 'withdrawn')
        ORDER BY score DESC, COALESCE(posted_at, last_seen_at) DESC
        """
    ).fetchall()
    # The CSV is the uncapped export; the Markdown shortlist is read top to
    # bottom, so one employer may fill at most its per-employer share of it.
    jobs = ranked[:limit]
    shortlist, hidden = cap_per_company(ranked, limit)
    generated = now_iso()
    lines = [
        "# Opportunity shortlist",
        "",
        f"Generated `{generated}` from source data. Scores are ranking hints, not facts.",
        "",
        "## Top matches",
        "",
    ]
    if not shortlist:
        lines.append("No active postings yet. Run `python3 pipeline.py run` or import login-only results.")
    shown: dict[str, int] = {}
    for index, job in enumerate(shortlist, start=1):
        reasons = json.loads(job["score_explanation"])
        reasons_for_display = display_reasons(reasons)
        lines.extend(
            [
                f"### {index}. [{job['title']}]({job['url']}) — {job['company']} ({job['score']}/100)",
                "",
                f"- Location: {job['location'] or 'not provided'}",
                f"- Type: {job['role_type']} · Status: {job['status']}",
                f"- Source: {job['source_name']} · Freshness: {stale_label(job['last_seen_at'], stale_days)}",
                f"- Why ranked here: {'; '.join(reasons_for_display) or 'base score only'}",
                f"- Pipeline ID: `{job['id']}`",
                "",
            ]
        )
        key = sort_key(job["company"])
        shown[key] = shown.get(key, 0) + 1
        if key in hidden and shown[key] == RANKED_VIEW_PER_COMPANY:
            # Said at the employer's last listed posting, never left silent.
            lines.extend(
                [
                    f"*+{hidden[key]} more from {job['company']}, not listed here: the shortlist "
                    f"shows each employer's top {RANKED_VIEW_PER_COMPANY}. The web dashboard's "
                    f"\"+{hidden[key]} more\" button on this employer lists them all.*",
                    "",
                ]
            )
    lines.extend(["## Manual check queue", ""])
    for item in sources_config.get("manual_check_sources", []):
        cadence = item.get("cadence", "weekly")
        lines.append(f"- [{item['name']}]({item['url']}) — {cadence}; {item.get('note', '')}".rstrip())
    lines.extend(
        [
            "",
            "## Next actions",
            "",
            "1. Open the top roles and verify eligibility/deadline at the source.",
            "2. Mark a role: `python3 pipeline.py update <ID> shortlisted`.",
            "3. Add login-only finds to `data/manual_jobs.csv`, then rerun the pipeline.",
            "4. Never treat an aggregator copy as authoritative; apply on the employer or university page.",
            "",
        ]
    )
    paths.OUTPUT_MD.parent.mkdir(parents=True, exist_ok=True)
    paths.OUTPUT_MD.write_text("\n".join(lines), encoding="utf-8")

    with paths.OUTPUT_CSV.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["id", "score", "status", "company", "title", "location", "role_type", "url", "source", "last_seen_at"]
        )
        for job in jobs:
            writer.writerow(
                [
                    job["id"],
                    job["score"],
                    job["status"],
                    job["company"],
                    job["title"],
                    job["location"],
                    job["role_type"],
                    job["url"],
                    job["source_name"],
                    job["last_seen_at"],
                ]
            )
    print(
        f"Wrote {len(shortlist)} matches to {paths.OUTPUT_MD.relative_to(paths.ROOT)} "
        f"(top {RANKED_VIEW_PER_COMPANY} per employer) and {len(jobs)} to {paths.OUTPUT_CSV.relative_to(paths.ROOT)}"
    )
    return len(jobs)


def build_dashboard_html(jobs: list[dict[str, Any]], generated_at: str) -> str:
    # Escaping "</" prevents a job title/description containing "</script>"
    # from breaking out of the embedded JSON data block.
    payload = json.dumps(jobs).replace("</", "<\\/")
    template = (paths.TEMPLATE_DIR / "dashboard.html").read_text(encoding="utf-8")
    doc = template.replace("GENERATED_AT_PLACEHOLDER", html.escape(generated_at))
    doc = doc.replace("JOB_DATA_PLACEHOLDER", payload)
    return doc


def render_dashboard(
    conn: sqlite3.Connection,
    sources_config: dict[str, Any],
    dashboard_limit: int,
    profile: dict[str, Any] | None = None,
) -> int:
    stale_days = int(sources_config.get("stale_after_days", 7))
    rows = conn.execute(
        """
        SELECT * FROM jobs
        WHERE active=1 AND duplicate_of IS NULL
        ORDER BY score DESC, COALESCE(posted_at, last_seen_at) DESC
        LIMIT ?
        """,
        (dashboard_limit,),
    ).fetchall()
    payload = [
        {
            "id": row["id"],
            "title": row["title"],
            "company": row["company"],
            "location": row["location"],
            "region": region_label(row["location"], profile or {}),
            "role_type": row["role_type"],
            "status": row["status"],
            "score": row["score"],
            "reasons": display_reasons(json.loads(row["score_explanation"])),
            "source_name": row["source_name"],
            "url": row["url"],
            "first_seen_at": row["first_seen_at"],
            "last_seen_at": row["last_seen_at"],
            "posted_at": row["posted_at"],
            "freshness": stale_label(row["last_seen_at"], stale_days),
        }
        for row in rows
    ]
    paths.OUTPUT_DASHBOARD.parent.mkdir(parents=True, exist_ok=True)
    paths.OUTPUT_DASHBOARD.write_text(build_dashboard_html(payload, now_iso()), encoding="utf-8")
    print(f"Wrote {len(payload)} opportunities to {paths.OUTPUT_DASHBOARD.relative_to(paths.ROOT)}")
    return len(payload)


def update_status(
    conn: sqlite3.Connection,
    job_id: str,
    status: str,
    notes: str | None,
    follow_up: str | None,
) -> None:
    if status not in VALID_STATUSES:
        raise SystemExit(f"Invalid status. Choose one of: {', '.join(sorted(VALID_STATUSES))}")
    existing = conn.execute("SELECT id FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not existing:
        raise SystemExit(f"No job found with ID {job_id}")
    applied_at = now_iso() if status == "applied" else None
    conn.execute(
        """
        UPDATE jobs
        SET status=?,
            notes=COALESCE(?, notes),
            follow_up_at=COALESCE(?, follow_up_at),
            applied_at=CASE WHEN ?='applied' THEN COALESCE(applied_at, ?) ELSE applied_at END
        WHERE id=?
        """,
        (status, notes, follow_up, status, applied_at, job_id),
    )
    conn.commit()
    # Plain ASCII arrow: the Windows console defaults to cp1252, which has no
    # mapping for U+2192, so an arrow here crashed `update` with a
    # UnicodeEncodeError before it could print the confirmation.
    print(f"Updated {job_id} -> {status}")


def show_status(conn: sqlite3.Connection) -> None:
    totals = conn.execute(
        "SELECT status, COUNT(*) AS count FROM jobs GROUP BY status ORDER BY count DESC"
    ).fetchall()
    active = conn.execute("SELECT COUNT(*) FROM jobs WHERE active=1 AND duplicate_of IS NULL").fetchone()[0]
    print(f"{active} active unique postings")
    for row in totals:
        print(f"  {row['status']}: {row['count']}")
    errors = conn.execute(
        """
        SELECT run.source_key, run.finished_at, run.error
        FROM fetch_runs AS run
        JOIN (
            SELECT source_key, MAX(id) AS latest_id
            FROM fetch_runs
            GROUP BY source_key
        ) AS latest ON latest.latest_id=run.id
        WHERE run.outcome='error'
        ORDER BY run.id DESC
        """
    ).fetchall()
    if errors:
        print("Recent source errors:")
        for row in errors:
            print(f"  {row['source_key']} at {row['finished_at']}: {row['error']}")


def doctor(profile: dict[str, Any], sources_config: dict[str, Any]) -> int:
    exit_code = 0
    missing: list[str] = []
    for key in (
        "graduation_year",
        "preferred_locations",
        "regions",
        "skills",
        "interest_keywords",
        "work_authorized_us",
        "requires_sponsorship",
        "hours_per_week",
        "available_terms",
        "compensation_preferences",
    ):
        value = profile.get(key)
        # preferred_locations is only the fallback for a profile with no regions.
        if key == "preferred_locations" and profile.get("regions"):
            continue
        if value is None or value == []:
            missing.append(key)
    if missing:
        print("Profile is usable but incomplete:")
        for key in missing:
            print(f"  - {key}")
        print("Edit config/profile.json, or ask your agent to follow SETUP.md.")
        exit_code = 1

    usajobs_sources = [
        source
        for source in sources_config.get("ats_sources", [])
        if source.get("kind") == "usajobs" and source.get("enabled", True)
    ]
    if usajobs_sources and not os.environ.get("USAJOBS_API_KEY"):
        print("USAJOBS source is enabled but USAJOBS_API_KEY is not set.")
        print("Register a free key at https://developer.usajobs.gov/, then copy .env.example")
        print("to .env and fill it in (.env is gitignored).")
        exit_code = 1
    # The API rejects requests whose User-Agent is not the address the key was
    # registered under, so a missing email fails just as hard as a missing key.
    if usajobs_sources and not os.environ.get("USAJOBS_CONTACT_EMAIL"):
        if any(not source.get("contact_email") for source in usajobs_sources):
            print("USAJOBS source is enabled but no contact email is set.")
            print("Set USAJOBS_CONTACT_EMAIL in .env to the address the key was registered with.")
            exit_code = 1

    adzuna_sources = [
        source
        for source in sources_config.get("ats_sources", [])
        if source.get("kind") == "adzuna" and source.get("enabled", True)
    ]
    # Adzuna issues the pair together and rejects a request missing either half,
    # so both are reported rather than only the first one found missing.
    adzuna_missing = [
        name for name in ("ADZUNA_APP_ID", "ADZUNA_APP_KEY") if not os.environ.get(name)
    ]
    if adzuna_sources and adzuna_missing:
        print(f"Adzuna source is enabled but {' and '.join(adzuna_missing)} not set.")
        print("Register a free application at https://developer.adzuna.com/, then copy")
        print(".env.example to .env and fill both values in (.env is gitignored).")
        exit_code = 1

    if exit_code == 0:
        print("Profile has all high-impact fields.")
    return exit_code
