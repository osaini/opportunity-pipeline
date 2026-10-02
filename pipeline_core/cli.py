"""Local, source-linked internship discovery and application pipeline."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import paths
from .artifacts import write_artifact
from .config import load_env_file, load_json, load_profile, load_sources
from .discovery import DISCOVERY_VENDOR_ORDER, report_discovery
from .fetch import fetch_all
from .importers import enrich_descriptions, import_discovered, import_emails, import_manual
from .liveness import check_liveness
from .reports import doctor, render_dashboard, report, show_status, update_status
from .retention import purge_expired
from .scoring import score_all
from .store import connect


# `run` exits with this (EX_TEMPFAIL) when a source could not be reached at all,
# so the scheduled wrapper knows the fetch is incomplete and retries it later
# instead of recording the day as done.
EXIT_TEMPFAIL = 75


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    resume_help = "Skip sources already fetched successfully since this ISO-8601 time"
    fetch_parser = sub.add_parser("fetch", help="Fetch enabled public ATS sources")
    fetch_parser.add_argument("--resume-since", help=resume_help)
    import_parser = sub.add_parser("import-csv", help="Import login-only or manually found postings")
    import_parser.add_argument("path", nargs="?", default=str(paths.MANUAL_PATH))
    import_email_parser = sub.add_parser(
        "import-emails", help="Import LinkedIn job-alert email JSON (see docs/guide/sources.md)"
    )
    import_email_parser.add_argument("path", nargs="?", default=str(paths.EMAIL_IMPORT_PATH))
    import_discovered_parser = sub.add_parser(
        "import-discovered",
        help="Import agent-discovered postings from search/public pages/lists (see docs/guide/sources.md)",
    )
    import_discovered_parser.add_argument("path", nargs="?", default=str(paths.DISCOVERED_IMPORT_PATH))
    enrich_parser = sub.add_parser(
        "enrich", help="Backfill descriptions an agent read from public posting pages"
    )
    enrich_parser.add_argument("path", nargs="?", default=str(paths.ENRICHMENT_PATH))
    enrich_parser.add_argument(
        "--force",
        action="store_true",
        help="Replace existing descriptions too, not just thin ones",
    )
    sub.add_parser("score", help="Recompute transparent fit scores")
    report_parser = sub.add_parser("report", help="Write Markdown, CSV, and dashboard shortlists")
    report_parser.add_argument("--limit", type=int, default=30)
    report_parser.add_argument("--dashboard-limit", type=int, default=300)
    run_parser = sub.add_parser("run", help="Fetch, import, score, and report")
    run_parser.add_argument("--limit", type=int, default=30)
    run_parser.add_argument("--dashboard-limit", type=int, default=300)
    run_parser.add_argument("--resume-since", help=resume_help)
    update_parser = sub.add_parser("update", help="Update application status")
    update_parser.add_argument("job_id")
    update_parser.add_argument("status")
    update_parser.add_argument("--notes")
    update_parser.add_argument("--follow-up", help="ISO date, e.g. 2026-08-05")
    resume_parser = sub.add_parser(
        "resume", help="Render your resume, optionally emphasised for one posting"
    )
    resume_parser.add_argument("--job", help="Posting ID to tailor emphasis toward")
    resume_parser.add_argument("--pdf", action="store_true", help="Also render a PDF")
    cover_parser = sub.add_parser("cover-letter", help="Draft a cover letter for one posting")
    cover_parser.add_argument("--job", required=True, help="Posting ID (see output/shortlist.md)")
    cover_parser.add_argument("--pdf", action="store_true", help="Also render a PDF")
    discover_parser = sub.add_parser(
        "discover-ats",
        help="Resolve company names to Greenhouse/Ashby/Lever boards (preview by default)",
    )
    discover_parser.add_argument("companies", nargs="*", help="Company names to probe")
    discover_parser.add_argument(
        "--in",
        dest="companies_path",
        help="JSON file holding a list of company names, or {\"companies\": [...]}",
    )
    discover_parser.add_argument(
        "--vendors",
        help=f"Comma-separated subset of {','.join(DISCOVERY_VENDOR_ORDER)}",
    )
    discover_parser.add_argument(
        "--write",
        action="store_true",
        help="Append identity-confirmed entries to config/sources.local.json",
    )
    discover_parser.add_argument(
        "--shared",
        action="store_true",
        help="With --write, append to the tracked shared catalog config/sources.json instead",
    )
    discover_parser.add_argument(
        "--include-unverified",
        action="store_true",
        help="Also write Ashby/Lever hits, whose APIs expose no company name to check",
    )
    liveness_parser = sub.add_parser(
        "liveness",
        help="Check whether imported postings are still open, and retire dead ones",
    )
    liveness_parser.add_argument(
        "--limit", type=int, help="Check at most N postings, least recently seen first"
    )
    liveness_parser.add_argument(
        "--all",
        action="store_true",
        dest="check_all",
        help="Also check ATS-sourced rows, which their own source batch already retires",
    )
    liveness_parser.add_argument(
        "--dry-run", action="store_true", help="Report verdicts without retiring anything"
    )
    purge_parser = sub.add_parser(
        "purge-expired",
        help="Delete retired postings and postings past their stated deadline",
    )
    purge_parser.add_argument(
        "--dry-run", action="store_true", help="Report what would be deleted without deleting"
    )
    sub.add_parser("status", help="Show pipeline counts and recent fetch errors")
    sub.add_parser("doctor", help="Check whether high-impact profile fields are filled")
    return parser


def main() -> int:
    # Company names and job titles come from scraped pages and routinely carry
    # characters the Windows console's cp1252 default cannot encode ("Ørsted",
    # curly quotes, typographic dashes). Printing one raises UnicodeEncodeError
    # mid-command, which would abort a run that was otherwise succeeding. The
    # test suite guards the literals in this file; only this guards the data.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, OSError):
            # Not a real console (piped, captured in tests): nothing to fix.
            pass

    args = build_parser().parse_args()
    load_env_file()
    profile = load_profile()
    sources = load_sources()
    conn = connect()
    try:
        if args.command == "fetch":
            if fetch_all(conn, sources, args.resume_since):
                return EXIT_TEMPFAIL
        elif args.command == "import-csv":
            import_manual(conn, Path(args.path).expanduser().resolve())
        elif args.command == "import-emails":
            import_emails(conn, Path(args.path).expanduser().resolve())
        elif args.command == "import-discovered":
            import_discovered(conn, Path(args.path).expanduser().resolve())
        elif args.command == "enrich":
            enrich_descriptions(conn, Path(args.path).expanduser().resolve(), args.force)
        elif args.command == "score":
            score_all(conn, profile)
        elif args.command == "report":
            report(conn, sources, args.limit)
            render_dashboard(conn, sources, args.dashboard_limit, profile)
        elif args.command == "run":
            transient_failures = fetch_all(conn, sources, args.resume_since)
            # Score and report even when some sources were unreachable, so the
            # shortlist reflects what did arrive; the exit code asks for a retry.
            import_manual(conn, paths.MANUAL_PATH)
            score_all(conn, profile)
            report(conn, sources, args.limit)
            render_dashboard(conn, sources, args.dashboard_limit, profile)
            if transient_failures:
                print(
                    f"{transient_failures} source(s) were unreachable; rerun with "
                    "--resume-since to fetch only what is missing",
                    file=sys.stderr,
                )
                return EXIT_TEMPFAIL
        elif args.command == "update":
            update_status(conn, args.job_id, args.status, args.notes, args.follow_up)
        elif args.command in {"resume", "cover-letter"}:
            write_artifact(conn, args.command, args.job, args.pdf)
        elif args.command == "discover-ats":
            companies = list(args.companies)
            if args.companies_path:
                payload = load_json(Path(args.companies_path).expanduser().resolve())
                listed = payload["companies"] if isinstance(payload, dict) else payload
                companies += [str(name).strip() for name in listed if str(name).strip()]
            if not companies:
                raise SystemExit("No company names given. Pass them as arguments or via --in.")
            report_discovery(
                companies,
                sources,
                args.write,
                args.include_unverified,
                args.vendors.split(",") if args.vendors else None,
                shared=args.shared,
            )
        elif args.command == "liveness":
            check_liveness(conn, args.limit, args.check_all, args.dry_run)
        elif args.command == "purge-expired":
            purge_expired(conn, dry_run=args.dry_run)
        elif args.command == "status":
            show_status(conn)
        elif args.command == "doctor":
            return doctor(profile, sources)
    finally:
        conn.close()
    return 0
