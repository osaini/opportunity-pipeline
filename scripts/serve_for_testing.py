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
something to show. It seeds one fictional Lever role too (Harbor Demo Labs, saved), served by a fake page client, with Apply for
me on Lever switched on, so the Lever "what's missing" view has something to show. Lever's Finish in browser returns a canned handoff with no window here, like Greenhouse's.
It is the only action a Lever role has: no rehearsal, no Look up options, no Submit. "Let the app attach my résumé on Lever" starts off, as
it does for every student: turn it on in Profile, under Automation, in the Applications list to see the start say that the app attaches the résumé and the run record
it, and see "Your application was not sent. Lever received your résumé." when you press Stop in the student's turn. A rehearsal or an option lookup on
the Greenhouse role returns a canned result after a few seconds, with a canned
picture. Finish in browser returns a canned handoff the same way, with no window: the fictional student's turn lasts
``apply_fake_ats.CANNED["handoff"]["wait"]`` seconds (1.5 by default), then the canned form answers by
``CANNED["handoff"]["outcome"]`` (submitted, unconfirmed, security_code, refused, failed_4xx or hang_after_hand_over).
The sandbox's fake board is not Acme's, so a start needs the student's word that the posting is right
(``posting_confirmed``). Stop and Bring the window forward work as in the real thing. Nothing reaches Greenhouse and no
browser opens.

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

from helpers_platform import build_and_migrate_fresh  # noqa: E402
from apply_fake_ats import JOB_URL, seed_lever_role  # noqa: E402
from sandbox_app import build_sandbox_app  # noqa: E402

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
    already has saved; its posting address becomes a Greenhouse job the fake listing describes. A second, fictional role, Harbor Demo
    Labs, is saved as a Lever posting that the fake page client serves (nothing is read from Lever). Turning the switches on happens
    after the app is built (the first one's requirement asks the app's agent factory).
    """
    from opportunity_app.student.profile import update_profile
    from opportunity_app.core.schema import LOCAL_USER_ID
    from opportunity_app.core.timestamps import utc_now

    conn = sqlite3.connect(platform_path)
    conn.row_factory = sqlite3.Row
    try:
        with conn:
            conn.execute("UPDATE opportunities SET url=? WHERE company='Acme Robotics'", (JOB_URL,))
        with conn:
            seed_lever_role(conn, LOCAL_USER_ID)
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
    _, platform_path = build_and_migrate_fresh(root)
    fake_apply = fake_apply_enabled()
    if fake_apply:
        seed_fake_apply(platform_path, root / "resumes")
    app = build_sandbox_app(
        root,
        platform_path,
        owner_token=OWNER_TOKEN,
        employer_token=EMPLOYER_TOKEN,
        admin_token=ADMIN_TOKEN,
        fake_apply=fake_apply,
        # A throwaway database: recovery codes come back in the response so an exploring agent can walk the flow.
        recovery_sandbox=True,
    )
    if fake_apply:
        from opportunity_app.automation import ledger as automation
        from opportunity_app.core.schema import LOCAL_USER_ID

        conn = sqlite3.connect(platform_path)
        conn.row_factory = sqlite3.Row
        try:
            automation.set_mode(conn, LOCAL_USER_ID, "apply_agent", "on")
            # The Lever role needs its own switch. The résumé choice stays off, as it is for every student until they turn it on.
            automation.set_mode(conn, LOCAL_USER_ID, "apply_agent_lever", "on")
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
