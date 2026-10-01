"""The sandbox app: create_app wired to offline fakes, shared by the browser suite and scripts/serve_for_testing.py.

Both need the same ~22 keyword arguments, and a new create_app dependency that needs a fake has to be added once, here,
or the sandbox or fuzz server would reach the real one. Not a test module.

What differs between the two callers is a parameter: the tokens, ``fake_apply`` (the browser suite always runs Apply for me
against the fictional listing; the sandbox does so only under PIPELINE_SANDBOX_FAKE_APPLY, and with it off wires no schema
client or agent, which the API fuzzer relies on) and ``recovery_sandbox`` (the sandbox hands recovery codes back in the
response so an exploring agent can walk the flow; the browser suite must keep them hidden).
"""

from __future__ import annotations

import os
from pathlib import Path

from opportunity_app import STATIC_DIR
from opportunity_app.api import create_app

import outreach_fakes
from apply_fake_ats import FakeApplyAgentFactory, FakeSchemaClient

REPO_ROOT = Path(__file__).resolve().parents[2]


def build_sandbox_app(
    root: Path,
    platform_path: Path,
    *,
    owner_token: str,
    employer_token: str,
    admin_token: str,
    fake_apply: bool,
    recovery_sandbox: bool,
):
    """create_app over a throwaway database under ``root``, with nothing that reaches a model, a website or a mailbox."""
    # A fixed compose account, set before the app reads .env (which never overrides a variable already set), keeps
    # personal settings out of the sandbox; approved outreach drafts open a Gmail compose link for this account.
    os.environ["PIPELINE_OUTREACH_COMPOSE"] = "gmail"
    os.environ["PIPELINE_OUTREACH_ACCOUNT"] = outreach_fakes.COMPOSE_ACCOUNT
    return create_app(
        # With a throwaway database, recovery codes may come back in the response; a real copy never does this.
        recovery_sandbox=recovery_sandbox,
        db_path=platform_path,
        access_token=owner_token,
        employer_token=employer_token,
        admin_token=admin_token,
        static_dir=STATIC_DIR,
        # Uploads must land in the temp tree, never in the repo's data/ directory.
        resume_storage=root / "resumes",
        capture_storage=root / "captures",
        interview_storage=root / "mock-interviews",
        # The Programs tab reads a student's own list; this one is invented.
        early_programs_file=REPO_ROOT / "tests" / "fixtures" / "early_programs.json",
        # Every caller shares 127.0.0.1, so the per-IP sliding window sees them as one client and would start returning 429
        # (masking the failures a fuzzer or an exploring agent is looking for). The limiter is covered by the unittest suite.
        rate_limit_per_minute=1_000_000,
        # Outreach drafting, contact finding and the deep search answer from offline fakes.
        outreach_provider_factory=outreach_fakes.provider_factory,
        outreach_draft_provider="anthropic",
        outreach_contact_client_factory=outreach_fakes.contact_client,
        outreach_contact_delay=0,
        outreach_discovery_manager=outreach_fakes.discovery_manager(platform_path, root / "outreach-reports"),
        outreach_recontact_manager=outreach_fakes.recontact_manager(platform_path),
        system_status=outreach_fakes.system_status(root / "system-status"),
        board_tracker=outreach_fakes.board_tracker(root / "boards"),
        outreach_settings=outreach_fakes.outreach_settings(root),
        # Jev review endpoints stay deterministic and offline too, which matters most for fuzzing.
        typesafe_client_factory=outreach_fakes.FakeTypeSafeClient,
        inbox_client_factory=outreach_fakes.FakeTypeSafeClient,
        # Apply for me: the fictional listing (any board, any job) and an agent that only says a window could open.
        apply_schema_client_factory=(lambda: FakeSchemaClient(any_job=True)) if fake_apply else None,
        apply_agent_factory=FakeApplyAgentFactory() if fake_apply else None,
    )
