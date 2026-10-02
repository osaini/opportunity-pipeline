"""What outreach reads from the environment: the sending address, the provider settings, and the Gmail links built from them.

A leaf: it imports only the standard library and agent_providers (itself standard library only), never outreach, so
the Gmail transport, the inbox reader, the schedulers and the drafting module can all import it at the top.

Every reader looks these up in os.environ when it runs (outreach_settings writes a change to .env and to
os.environ), so nothing here caches a value.
"""

from __future__ import annotations

import os
from urllib.parse import quote

from ..integrations.agent_providers import default_provider, provider_catalog

ACCOUNT_ENV = "PIPELINE_OUTREACH_ACCOUNT"
DRAFT_ENV = "PIPELINE_OUTREACH_PROVIDER"
RESEARCH_ENV = "PIPELINE_OUTREACH_DISCOVERY_PROVIDER"
COMPANY_RESEARCH_ENV = "PIPELINE_OUTREACH_COMPANY_RESEARCH_PROVIDER"
FOLLOW_UP_ENV = "PIPELINE_OUTREACH_FOLLOW_UP_PROVIDER"
CALL_PREP_ENV = "PIPELINE_OUTREACH_CALL_PREP_PROVIDER"
REVIEW_ENV = "PIPELINE_OUTREACH_REVIEW_PROVIDER"
# The thank-you after a decline (outreach_thank_you); empty means the first-email writer.
THANK_YOU_ENV = "PIPELINE_OUTREACH_THANK_YOU_PROVIDER"
# Set to 1 in .env to let Codex read web pages for company research and the deep search. Off by default: the only way to
# give Codex a web tool also switches on code mode, which Codex cannot be stopped from exposing (see agent_providers.codex_command).
ALLOW_CODEX_ENV = "PIPELINE_OUTREACH_RESEARCH_ALLOW_CODEX"
ATTACHMENT_ENV = "PIPELINE_OUTREACH_ATTACHMENT"
LINKEDIN_ENV = "PIPELINE_LINKEDIN_ACCOUNT"

# What each writer reads before falling back to the first-email drafts setting.
PURPOSE_ENV = {
    "follow_up": FOLLOW_UP_ENV,
    "call_prep": CALL_PREP_ENV,
    "thank_you": THANK_YOU_ENV,
}


def sender_account() -> str:
    """The Gmail address the student sends from, or "" when it is not set."""
    return os.environ.get(ACCOUNT_ENV, "").strip()


def discovery_provider() -> str:
    """The CLI that does the web research, "claude-code" when none is chosen."""
    return os.environ.get(RESEARCH_ENV) or "claude-code"


def codex_web_allowed() -> bool:
    """Whether the student accepted, in .env, that Codex reads web pages for research (ALLOW_CODEX_ENV)."""
    return os.environ.get(ALLOW_CODEX_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def resolve_provider(requested: str | None = None, purpose: str = "initial") -> tuple[str, str]:
    """Pick the provider and model for one kind of writing.

    Explicit, then the setting for this purpose (follow-ups, call prep, and the
    thank-you after a decline have their own), then PIPELINE_OUTREACH_PROVIDER, then the first provider that
    is set up on this computer.
    """
    own = os.environ.get(PURPOSE_ENV[purpose], "") if purpose in PURPOSE_ENV else ""
    provider = (requested or own or os.environ.get(DRAFT_ENV, "") or default_provider()).strip()
    if provider == "legacy":
        return "legacy", ""
    record = next((item for item in provider_catalog() if item["id"] == provider), None)
    if record is None:
        raise ValueError("Draft provider must be openai, anthropic, claude-code, codex-cli, or legacy")
    return provider, str(record["model"])


def gmail_web_url(fragment: str, account: str | None = None) -> str:
    """A link into the student's Gmail on the web (the outreach account, else the first signed in), to a thread or a draft.

    ``account`` is the address to open, "" for the first signed-in account; left out, it is the outreach account.
    """
    if account is None:
        account = sender_account()
    return f"https://mail.google.com/mail/?authuser={quote(account) if account else '0'}#{fragment}"
