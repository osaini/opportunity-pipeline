"""Serve the app against a disposable, seeded database.

This is the target for anything that needs a real running instance rather than
an in-process TestClient:

  * exploratory bug hunting through the Playwright MCP server
  * schemathesis fuzzing of the generated OpenAPI schema
  * poking at the UI by hand without touching data/platform.db

The database is rebuilt from the same fixture the unittest suite uses, so the
contents are predictable and nothing here can reach real data. Tokens are fixed
and printed on startup.

    py -3 scripts/serve_for_testing.py --port 8799

Stop it with Ctrl+C; the temporary directory is removed on exit.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tests"))
sys.path.insert(0, str(REPO_ROOT / "tests" / "ui"))

import uvicorn  # noqa: E402

from opportunity_app import STATIC_DIR  # noqa: E402
from opportunity_app.api import create_app  # noqa: E402

from helpers_platform import build_and_migrate  # noqa: E402
import outreach_fakes  # noqa: E402

# Fixed so tooling and documentation can rely on them. They only ever guard a
# throwaway database on loopback.
OWNER_TOKEN = "sandbox-owner-token"
EMPLOYER_TOKEN = "sandbox-employer-token"
ADMIN_TOKEN = "sandbox-admin-token"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8799)
    parser.add_argument("--host", default="127.0.0.1", help="Loopback only; this app has no auth worth exposing.")
    parser.add_argument(
        "--keep",
        action="store_true",
        help="Leave the temporary database behind on exit for inspection.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        raise SystemExit("serve_for_testing binds loopback only: it uses well-known tokens.")

    root = Path(tempfile.mkdtemp(prefix="opportunity-sandbox-"))
    _, platform_path = build_and_migrate(root)
    # A fixed compose account, set before the app reads .env (which never
    # overrides a variable already set), keeps personal settings out of the sandbox.
    os.environ["PIPELINE_OUTREACH_COMPOSE"] = "gmail"
    os.environ["PIPELINE_OUTREACH_ACCOUNT"] = outreach_fakes.COMPOSE_ACCOUNT
    app = create_app(
        # A throwaway database: recovery codes come back in the response so an
        # exploring agent can walk the flow. A real copy never does this.
        recovery_sandbox=True,
        db_path=platform_path,
        access_token=OWNER_TOKEN,
        employer_token=EMPLOYER_TOKEN,
        admin_token=ADMIN_TOKEN,
        static_dir=STATIC_DIR,
        resume_storage=root / "resumes",
        capture_storage=root / "captures",
        interview_storage=root / "mock-interviews",
        # A fuzzer or an exploring agent will exceed the production window
        # immediately, and 429s would mask the failures worth finding.
        rate_limit_per_minute=1_000_000,
        # Outreach drafting, contact finding, and the deep search answer from
        # offline fakes, so exploring the sandbox never reaches a model or a website.
        outreach_provider_factory=outreach_fakes.provider_factory,
        outreach_draft_provider="anthropic",
        outreach_contact_client_factory=outreach_fakes.contact_client,
        outreach_contact_delay=0,
        outreach_discovery_manager=outreach_fakes.discovery_manager(platform_path, root / "outreach-reports"),
        outreach_recontact_manager=outreach_fakes.recontact_manager(platform_path),
        system_status=outreach_fakes.system_status(root / "system-status"),
        board_tracker=outreach_fakes.board_tracker(root / "boards"),
        outreach_settings=outreach_fakes.outreach_settings(root),
        # Jev review endpoints also stay deterministic and offline. This is
        # particularly important for fuzzing, which exercises every operation.
        typesafe_client_factory=outreach_fakes.FakeTypeSafeClient,
    )

    print(f"Sandbox app:    http://{args.host}:{args.port}")
    print(f"Sandbox data:   {root}")
    print(f"Owner token:    {OWNER_TOKEN}   (paste into the 'Owner invitation' field)")
    print(f"Employer token: {EMPLOYER_TOKEN}")
    print(f"Admin token:    {ADMIN_TOKEN}")
    try:
        uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    finally:
        if args.keep:
            print(f"Left sandbox database at {root}")
        else:
            shutil.rmtree(root, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
