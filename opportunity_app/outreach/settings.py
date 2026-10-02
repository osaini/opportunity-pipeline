"""The outreach settings a student would otherwise edit in .env by hand.

None of these values is secret:

- PIPELINE_OUTREACH_PROVIDER: who writes first-email drafts.
- PIPELINE_OUTREACH_FOLLOW_UP_PROVIDER, PIPELINE_OUTREACH_CALL_PREP_PROVIDER and
  PIPELINE_OUTREACH_THANK_YOU_PROVIDER: who writes follow-ups, call prep, and
  the thank-you after a decline; empty means the same as first emails.
- PIPELINE_OUTREACH_REVIEW_PROVIDER: who reviews a follow-up, or a thank-you
  after a decline, before it goes; empty means automatic
  (outreach_review.review_choice, per purpose).
- PIPELINE_OUTREACH_DISCOVERY_PROVIDER: which CLI does the web research (the
  deep search, placing companies, Find people, and searching other sites).
- PIPELINE_OUTREACH_COMPANY_RESEARCH_PROVIDER: which CLI researches one
  company for call prep; empty means the same as above.
- PIPELINE_OUTREACH_ATTACHMENT: the file attached to Gmail drafts.
- PIPELINE_LINKEDIN_ACCOUNT: the LinkedIn test account call prep may read
  interviewers' profiles as (outreach/linkedin.py); empty means LinkedIn is off.

Every choice lists what this computer can run, and says what is not set up.

Every reader looks these up in os.environ when it runs, so a change is written
to .env (kept for the next start) and to os.environ (in effect now), and no
restart is needed. API keys and passwords are not here: they stay in .env.
"""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
from pathlib import Path
from typing import Any

from .. import ROOT
from ..integrations.agent_providers import catalog_snapshot, default_provider, provider_catalog
from .config import (
    ALLOW_CODEX_ENV,
    ATTACHMENT_ENV,
    CALL_PREP_ENV,
    COMPANY_RESEARCH_ENV,
    DRAFT_ENV,
    FOLLOW_UP_ENV,
    LINKEDIN_ENV,
    RESEARCH_ENV,
    REVIEW_ENV,
    THANK_YOU_ENV,
    codex_web_allowed,
)
from .linkedin import username_from
from .gmail import attachment_path, attachment_problem
from .review import review_choice
from ..student.resumes import DEFAULT_STORAGE, ResumeNotFoundError, list_resumes, resume_file_path

# Writers that fall back to the first-email setting when left empty.
FOLLOWING_DRAFTS = {"follow_up_provider": FOLLOW_UP_ENV, "call_prep_provider": CALL_PREP_ENV, "thank_you_provider": THANK_YOU_ENV}
RESEARCH_AGENTS = ("claude-code", "codex-cli")
# What the research pickers show beside an installed Codex that may not read the web yet, in place of "not set up".
CODEX_NEEDS_OPT_IN = "needs the .env opt-in"
LEGACY_OPTION = {
    "id": "legacy", "label": "Grounded template (no AI)", "available": True,
    "hint": "Fills a fixed template from your confirmed facts; nothing is sent to a model.",
}
# Uploaded resumes are stored under opaque names; the copy a recipient sees
# keeps the name the student gave the file.
DEFAULT_ATTACHMENT_DIR = ROOT / "data" / "private" / "outreach-attachment"
SAFE_NAME = re.compile(r"[^A-Za-z0-9 ._()-]+")


class OutreachSettings:
    def __init__(
        self,
        *,
        env_path: Path = ROOT / ".env",
        attachment_dir: Path = DEFAULT_ATTACHMENT_DIR,
        resume_storage: Path = DEFAULT_STORAGE,
    ) -> None:
        self.env_path = env_path
        self.attachment_dir = attachment_dir
        self.resume_storage = resume_storage

    def view(self, conn: sqlite3.Connection, *, user_id: str) -> dict[str, Any]:
        # The writer list, both Automatic reviewers and the default writer all read the provider catalog.
        with catalog_snapshot():
            return self._view(conn, user_id=user_id)

    @staticmethod
    def _research_option(option: dict[str, Any]) -> dict[str, Any]:
        """Codex reads the web only after the .env opt-in (agents.codex_runner); until then, say so rather than offer it."""
        if option["id"] == "codex-cli" and option["available"] and not codex_web_allowed():
            return {**option, "available": False, "reason": CODEX_NEEDS_OPT_IN, "hint": (
                f"Codex cannot be limited to web search, so it reads the web only when {ALLOW_CODEX_ENV}=1 is set in .env. "
                "Until then Claude Code does this research when it is installed."
            )}
        return option

    def _view(self, conn: sqlite3.Connection, *, user_id: str) -> dict[str, Any]:
        catalog = provider_catalog()
        drafts = [
            {"id": item["id"], "label": item["display_name"], "available": item["configured"], "hint": item["setup_hint"]}
            for item in catalog
        ] + [LEGACY_OPTION]
        research = [self._research_option(option) for option in drafts if option["id"] in RESEARCH_AGENTS]
        attached = attachment_path()

        def automatic(purpose: str) -> dict[str, str]:
            try:
                chosen, note = review_choice(purpose)
            except ValueError as exc:
                return {"id": "", "note": "", "problem": str(exc)}
            return {"id": chosen, "note": note, "problem": ""}

        # Automatic avoids the writer's company, and follow-ups and thank-yous can have different writers.
        automatic_review = automatic("follow_up")
        automatic_thank_you_review = automatic("thank_you")
        following = {
            key: {"value": os.environ.get(env, "").strip(), "options": drafts}
            for key, env in FOLLOWING_DRAFTS.items()
        }
        return {
            **following,
            "review_provider": {
                "value": os.environ.get(REVIEW_ENV, "").strip(),
                # On Automatic: which model reviews now, and why: follow-ups, and thank-yous after a decline.
                "automatic": automatic_review,
                "automatic_thank_you": automatic_thank_you_review,
                "options": [option for option in drafts if option["id"] != "legacy"],
            },
            "draft_provider": {
                "value": os.environ.get(DRAFT_ENV, "").strip(),
                # With nothing set, drafting uses the first provider that is set up.
                "default": default_provider(),
                "options": drafts,
            },
            "research_agent": {
                "value": os.environ.get(RESEARCH_ENV, "").strip() or "claude-code",
                "options": research,
            },
            # Empty is the same agent as the deep search (outreach_research.research_agent).
            "company_research_agent": {
                "value": os.environ.get(COMPANY_RESEARCH_ENV, "").strip(),
                "options": research,
            },
            # Only the username is ever shown or kept: it is what the account check compares.
            "linkedin_account": {"value": username_from(os.environ.get(LINKEDIN_ENV, ""))},
            "attachment": {
                "name": attached.name if attached else "",
                "problem": attachment_problem(attached),
                "resumes": [
                    {"id": item["id"], "name": item["original_name"], "created_at": item["created_at"], "status": item["status"]}
                    for item in list_resumes(conn, user_id=user_id)
                ],
            },
        }

    def update(self, conn: sqlite3.Connection, changes: dict[str, Any], *, user_id: str) -> dict[str, Any]:
        from ..setup import set_env_values

        updates: dict[str, str] = {}
        if "draft_provider" in changes:
            value = str(changes["draft_provider"] or "").strip()
            known = {item["id"] for item in provider_catalog()} | {"legacy"}
            if value and value not in known:
                raise ValueError("Unknown draft writer")
            updates[DRAFT_ENV] = value
        known = {item["id"] for item in provider_catalog()}
        for key, env in FOLLOWING_DRAFTS.items():
            if key in changes:
                value = str(changes[key] or "").strip()
                if value and value not in known | {"legacy"}:
                    raise ValueError("Unknown writer")
                updates[env] = value
        if "review_provider" in changes:
            value = str(changes["review_provider"] or "").strip()
            if value and value not in known:
                raise ValueError("Unknown reviewer")
            updates[REVIEW_ENV] = value
        if "company_research_agent" in changes:
            value = str(changes["company_research_agent"] or "").strip()
            if value and value not in RESEARCH_AGENTS:
                raise ValueError("The company research agent must be claude-code, codex-cli, or empty")
            updates[COMPANY_RESEARCH_ENV] = value
        if "linkedin_account" in changes:
            raw = str(changes["linkedin_account"] or "").strip()
            value = username_from(raw)
            if raw and not re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,99}", value):
                raise ValueError("Give the LinkedIn account as its profile link or username, or leave it empty to turn LinkedIn off")
            updates[LINKEDIN_ENV] = value
        if "research_agent" in changes:
            value = str(changes["research_agent"] or "").strip()
            if value not in RESEARCH_AGENTS:
                raise ValueError("The research agent must be claude-code or codex-cli")
            updates[RESEARCH_ENV] = value
        if "attachment_resume_id" in changes:
            resume_id = str(changes["attachment_resume_id"] or "").strip()
            updates[ATTACHMENT_ENV] = self._attachment_for(conn, resume_id, user_id=user_id) if resume_id else ""
        if updates:
            set_env_values(self.env_path, updates, overwrite=True)
            os.environ.update(updates)
        return self.view(conn, user_id=user_id)

    def _attachment_for(self, conn: sqlite3.Connection, resume_id: str, *, user_id: str) -> str:
        try:
            path, original_name, _media_type = resume_file_path(conn, resume_id, self.resume_storage, user_id=user_id)
        except ResumeNotFoundError as exc:
            raise ValueError("That resume is no longer uploaded") from exc
        name = SAFE_NAME.sub("_", Path(original_name).name).strip(" .") or f"resume{path.suffix}"
        if Path(name).suffix.lower() != path.suffix.lower():
            name = f"{Path(name).stem}{path.suffix}"
        self.attachment_dir.mkdir(parents=True, exist_ok=True)
        # One attachment at a time: a stale copy would only confuse which file goes out.
        for stale in self.attachment_dir.iterdir():
            if stale.is_file():
                stale.unlink()
        target = self.attachment_dir / name
        shutil.copyfile(path, target)
        try:
            return target.relative_to(ROOT).as_posix()
        except ValueError:
            return str(target)
