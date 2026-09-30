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

PIPELINE_SANDBOX_FAKE_APPLY=1 also turns Apply for me on for the seeded student, with a fake Greenhouse
listing and a fake agent: Acme Robotics (saved) becomes a Greenhouse role, so the "what's missing" view has
something to show. Nothing reaches Greenhouse and no browser opens.

Stop it with Ctrl+C; the temporary directory is removed on exit.
"""

from __future__ import annotations

import argparse
import os
import hashlib
import shutil
import sqlite3
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
from apply_fake_ats import FakeApplyAgentFactory, FakeSchemaClient, JOB_URL  # noqa: E402

# Fixed so tooling and documentation can rely on them. They only ever guard a
# throwaway database on loopback.
OWNER_TOKEN = "sandbox-owner-token"
EMPLOYER_TOKEN = "sandbox-employer-token"
ADMIN_TOKEN = "sandbox-admin-token"
FAKE_APPLY_ENV = "PIPELINE_SANDBOX_FAKE_APPLY"


def fake_apply_enabled() -> bool:
    return os.environ.get(FAKE_APPLY_ENV, "").strip() == "1"


def seed_fake_apply(platform_path: Path, resume_root: Path) -> None:
    """Make the sandbox student ready for Apply for me: a Greenhouse role, a name for applications, an email, a résumé.

    All fictional, and only under PIPELINE_SANDBOX_FAKE_APPLY. The role is Acme Robotics, which the sandbox
    already has saved; its posting address becomes a Greenhouse job the fake listing describes. Turning the
    switch on happens after the app is built (its requirement asks the app's agent factory).
    """
    from opportunity_app.profile import update_profile
    from opportunity_app.schema import LOCAL_USER_ID, utc_now

    conn = sqlite3.connect(platform_path)
    conn.row_factory = sqlite3.Row
    try:
        with conn:
            conn.execute("UPDATE opportunities SET url=? WHERE company='Acme Robotics'", (JOB_URL,))
        update_profile(
            conn,
            {"name_parts": {"first": "Sam", "last": "Rivera", "preferred": ""}, "contact": {"email": "sam.rivera@example.test"}},
            ["name_parts", "contact"], user_id=LOCAL_USER_ID,
        )
        data = b"%PDF-1.4\n% a fictional sandbox resume\n"
        resume_root.mkdir(parents=True, exist_ok=True)
        (resume_root / "resume-file-sandbox.pdf").write_bytes(data)
        stamp = utc_now()
        with conn:
            conn.execute(
                "INSERT INTO resume_files(id, user_id, original_name, media_type, byte_size, sha256, storage_path, created_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                ("resume-file-sandbox", LOCAL_USER_ID, "Sam Rivera Resume.pdf", "application/pdf", len(data), hashlib.sha256(data).hexdigest(),
                 "resume-file-sandbox.pdf", stamp),
            )
            conn.execute(
                "INSERT INTO resume_versions(id, resume_file_id, user_id, extracted_text, parsed_json, confirmed_json, status, created_at, confirmed_at) "
                "VALUES(?, ?, ?, ?, '{}', '{}', 'confirmed', ?, ?)",
                ("resume-sandbox", "resume-file-sandbox", LOCAL_USER_ID, "Sam Rivera", stamp, stamp),
            )
    finally:
        conn.close()


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
    fake_apply = fake_apply_enabled()
    if fake_apply:
        seed_fake_apply(platform_path, root / "resumes")
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
        early_programs_file=REPO_ROOT / "tests" / "fixtures" / "early_programs.json",
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
        inbox_client_factory=outreach_fakes.FakeTypeSafeClient,
        # Apply for me: the fictional listing (any board, any job) and an agent that only says a window could open.
        apply_schema_client_factory=(lambda: FakeSchemaClient(any_job=True)) if fake_apply else None,
        apply_agent_factory=FakeApplyAgentFactory() if fake_apply else None,
    )
    if fake_apply:
        from opportunity_app import automation
        from opportunity_app.schema import LOCAL_USER_ID

        conn = sqlite3.connect(platform_path)
        conn.row_factory = sqlite3.Row
        try:
            automation.set_mode(conn, LOCAL_USER_ID, "apply_agent", "on")
        finally:
            conn.close()

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
