"""Outreach maintenance from the command line and scheduled tasks.

    python -m opportunity_app.outreach_cli discover [--scopes local-accelerators us-startups]
                                                   [--max 10] [--dry-run] [--trigger scheduled]
    python -m opportunity_app.outreach_cli locate [--limit 20] [--batch 8] [--provider claude-code]
    python -m opportunity_app.outreach_cli recontact [--apply] [--redraft] [--limit 20] [--no-email-search]
    python -m opportunity_app.outreach_cli enrich [--all] [--force] [--limit 20] [--no-sec] [--no-render]
    python -m opportunity_app.outreach_cli remind

``discover`` runs the deep search (see outreach_discovery.py), one search per
scope with up to --max companies each. With --dry-run it
only writes a timestamped data/outreach-discovered-<date>-<time>-<run>-dry-run.json report and changes no rows.
``enrich`` fills in where each company is based, from its own site and its SEC
Form D filings (see outreach_profile.py), for targets with no sourced location
or no Form D lookup yet. --all rechecks every target not checked in 30 days,
--force ignores the 30 days. A location you typed is never changed. Form D
lookups need PIPELINE_SEC_USER_AGENT ("Your Name you@example.com") in .env.
Sites that are empty without JavaScript are rendered in headless Chromium when
Playwright is installed (requirements-optional.txt); --no-render skips that.
``locate`` searches the web for the companies neither their site nor a filing
placed, through the same headless CLI the deep search uses. Python opens the
page the search cites and keeps the location only when that page loads, names
the company, and states the place (see outreach_locate.py). A deep search runs
this for its new companies too.
``recontact`` looks again for a person to write to at targets that have only a
shared inbox or no address, and are not sent or approved (see
outreach_recontact.py): it re-reads their site, asks the mail server about
guesses, and searches other sites. It reports what it would change; --apply
makes the change and --redraft rewrites unapproved drafts for the new recipient.
Guesses go to the mail server unless PIPELINE_OUTREACH_SMTP_VERIFY=0; this sends
no mail. A deep search does all of this for its new companies.
``remind`` queues in-app reminders for follow-ups that are due. None of them
sends mail.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from contextlib import ExitStack, closing
from pathlib import Path

from pipeline import load_env_file

from . import DEFAULT_PLATFORM_DB
from .agent_providers import build_provider
from .database import is_postgres_target
from .outreach import queue_follow_up_reminders
from .outreach_contacts import default_fetcher
from .outreach_discovery import DEFAULT_SCOPES, MAX_PER_SCOPE, RUNNERS, SCOPES, DiscoveryBusy, run_discovery
from .outreach_locate import BATCH_SIZE, locate_targets
from .outreach_profile import SEC_USER_AGENT_ENV, enrich_targets, sec_fetcher
from .outreach_recontact import recontact_targets
from .outreach_render import default_renderer
from .outreach_smtp import default_verifier
from .schema import LOCAL_USER_ID, connect_product, ensure_product_schema

# pipeline.py's temporary-failure exit code, so the daily script can tell a
# busy or skipped run from a broken one.
TEMPFAIL_EXIT = 75


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=str(DEFAULT_PLATFORM_DB), help="SQLite path or PostgreSQL URL")
    parser.add_argument("--user", default=LOCAL_USER_ID)
    commands = parser.add_subparsers(dest="command", required=True)
    discover = commands.add_parser("discover", help="Run the deep search for new outreach targets")
    discover.add_argument("--scopes", nargs="+", choices=sorted(SCOPES), default=list(DEFAULT_SCOPES))
    discover.add_argument("--max", type=int, default=MAX_PER_SCOPE, dest="max_targets", help="Companies per scope (at most 25)")
    discover.add_argument("--dry-run", action="store_true", help="Write the report only; change no rows")
    discover.add_argument("--no-locate", action="store_true", help="Skip the web search for new companies nothing else placed")
    discover.add_argument("--no-email-search", action="store_true", help="Skip searching other sites for a person's address")
    discover.add_argument("--trigger", choices=("manual", "scheduled"), default="manual")
    discover.add_argument(
        "--provider", choices=sorted(RUNNERS),
        default=(os.environ.get("PIPELINE_OUTREACH_DISCOVERY_PROVIDER") or "claude-code"),
        help="CLI that performs the web research",
    )
    enrich = commands.add_parser("enrich", help="Fill in company locations and SEC Form D filings")
    enrich.add_argument("--all", action="store_true", dest="all_targets", help="Recheck targets that already have a sourced location")
    enrich.add_argument("--force", action="store_true", help="Recheck targets checked in the last 30 days too")
    enrich.add_argument("--limit", type=int, default=None, help="Check at most this many targets")
    enrich.add_argument("--no-sec", action="store_true", help="Skip SEC Form D lookups")
    enrich.add_argument("--no-site", action="store_true", help="Skip reading company sites")
    enrich.add_argument("--no-render", action="store_true", help="Never render JavaScript-built sites in a browser")
    locate = commands.add_parser("locate", help="Search the web for the locations nothing else settled")
    locate.add_argument("--limit", type=int, default=None, help="Search for at most this many companies")
    locate.add_argument("--batch", type=int, default=BATCH_SIZE, help="Companies per search run")
    locate.add_argument(
        "--provider", choices=sorted(RUNNERS),
        default=(os.environ.get("PIPELINE_OUTREACH_DISCOVERY_PROVIDER") or "claude-code"),
        help="CLI that performs the web research",
    )
    recontact = commands.add_parser("recontact", help="Look again for a person to write to at shared-inbox targets")
    recontact.add_argument("--apply", action="store_true", help="Change the contacts; without it, only report")
    recontact.add_argument("--redraft", action="store_true", help="With --apply, rewrite unapproved drafts for the new recipient")
    recontact.add_argument("--limit", type=int, default=None, help="Check at most this many targets")
    recontact.add_argument("--target", nargs="+", default=None, dest="target_ids", help="Only these target ids")
    recontact.add_argument("--no-email-search", action="store_true", help="Skip searching other sites")
    recontact.add_argument("--no-render", action="store_true", help="Never render JavaScript-built sites in a browser")
    recontact.add_argument(
        "--provider", choices=sorted(RUNNERS),
        default=os.environ.get("PIPELINE_OUTREACH_DISCOVERY_PROVIDER", "claude-code"),
        help="CLI that performs the web research",
    )
    commands.add_parser("remind", help="Queue in-app reminders for due follow-ups")
    return parser


def main(argv: list[str] | None = None) -> int:
    load_env_file()
    args = build_parser().parse_args(argv)
    # connect_product takes a Path for SQLite and the URL itself for PostgreSQL.
    target = args.db if is_postgres_target(args.db) else Path(args.db)
    with closing(connect_product(target)) as conn:
        ensure_product_schema(conn)
        if args.command == "remind":
            print(json.dumps(queue_follow_up_reminders(conn)))
            return 0
        if args.command == "enrich":
            return _enrich(conn, args)
        if args.command == "locate":
            return _locate(conn, args)
        if args.command == "recontact":
            return _recontact(conn, args)
        try:
            with ExitStack() as stack:
                fetcher = stack.enter_context(default_fetcher())
                form_d = sec_fetcher()
                renderer = default_renderer()
                verifier = default_verifier()
                result = run_discovery(
                    conn,
                    user_id=args.user,
                    runner=RUNNERS[args.provider],
                    fetcher=fetcher,
                    scopes=args.scopes,
                    max_targets=args.max_targets,
                    dry_run=args.dry_run,
                    trigger=args.trigger,
                    provider_factory=build_provider,
                    form_d_fetcher=stack.enter_context(form_d) if form_d is not None else None,
                    renderer=stack.enter_context(renderer) if renderer is not None else None,
                    locate_runner=None if args.no_locate else RUNNERS[args.provider],
                    email_runner=None if args.no_email_search else RUNNERS[args.provider],
                    verifier=stack.enter_context(verifier) if verifier is not None else None,
                )
        except DiscoveryBusy as exc:
            print(str(exc), file=sys.stderr)
            return TEMPFAIL_EXIT
        except Exception as exc:  # noqa: BLE001 - the scheduled task logs this line
            print(f"Deep search failed: {exc}", file=sys.stderr)
            return 1
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def _locate(conn, args: argparse.Namespace) -> int:
    with default_fetcher() as fetcher:
        result = locate_targets(
            conn, user_id=args.user, runner=RUNNERS[args.provider], fetcher=fetcher,
            limit=args.limit, batch_size=args.batch,
        )
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def _recontact(conn, args: argparse.Namespace) -> int:
    renderer = None if args.no_render else default_renderer()
    verifier = default_verifier()
    with ExitStack() as stack:
        fetcher = stack.enter_context(default_fetcher())
        result = recontact_targets(
            conn,
            user_id=args.user,
            fetcher=fetcher,
            runner=None if args.no_email_search else RUNNERS[args.provider],
            verifier=stack.enter_context(verifier) if verifier is not None else None,
            renderer=stack.enter_context(renderer) if renderer is not None else None,
            target_ids=args.target_ids,
            limit=args.limit,
            apply=args.apply,
            redraft=args.redraft,
            provider_factory=build_provider,
        )
    print(json.dumps(result, indent=2, ensure_ascii=False))
    if not args.apply and result["upgraded"]:
        print(f"{result['upgraded']} targets would get a new contact; run again with --apply to change them.", file=sys.stderr)
    return 0


def _enrich(conn, args: argparse.Namespace) -> int:
    form_d = None if args.no_sec else sec_fetcher()
    if form_d is None and not args.no_sec:
        print(f"SEC Form D lookups are off: set {SEC_USER_AGENT_ENV} in .env to your name and email.", file=sys.stderr)
    renderer = None if args.no_site or args.no_render else default_renderer()
    with ExitStack() as stack:
        site = None if args.no_site else stack.enter_context(default_fetcher())
        if renderer is not None:
            stack.enter_context(renderer)
        result = enrich_targets(
            conn,
            user_id=args.user,
            site_fetcher=site,
            form_d_fetcher=stack.enter_context(form_d) if form_d is not None else None,
            only_missing=not args.all_targets,
            force=args.force,
            limit=args.limit,
            renderer=renderer,
        )
    if renderer is not None and renderer.unavailable:
        print(f"JavaScript-built sites were not rendered: {renderer.unavailable}", file=sys.stderr)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
