"""The outreach settings a student would otherwise edit in .env by hand.

None of these values is secret:

- PIPELINE_OUTREACH_PROVIDER: who writes first-email drafts.
- PIPELINE_OUTREACH_FOLLOW_UP_PROVIDER and PIPELINE_OUTREACH_CALL_PREP_PROVIDER:
  who writes follow-ups and call prep; empty means the same as first emails.
- PIPELINE_OUTREACH_REVIEW_PROVIDER: who reviews a follow-up before it goes;
  empty means automatic (outreach_review.review_choice).
- PIPELINE_OUTREACH_DISCOVERY_PROVIDER: which CLI does the web research (the
  deep search, placing companies, Find people, and searching other sites).
- PIPELINE_OUTREACH_ATTACHMENT: the file attached to Gmail drafts.

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

from . import ROOT
from .agent_providers import default_provider, provider_catalog
from .outreach_gmail import attachment_path, attachment_problem
from .resumes import DEFAULT_STORAGE, ResumeNotFoundError, list_resumes, resume_file_path

DRAFT_ENV = "PIPELINE_OUTREACH_PROVIDER"
RESEARCH_ENV = "PIPELINE_OUTREACH_DISCOVERY_PROVIDER"
FOLLOW_UP_ENV = "PIPELINE_OUTREACH_FOLLOW_UP_PROVIDER"
CALL_PREP_ENV = "PIPELINE_OUTREACH_CALL_PREP_PROVIDER"
REVIEW_ENV = "PIPELINE_OUTREACH_REVIEW_PROVIDER"
# Writers that fall back to the first-email setting when left empty.
FOLLOWING_DRAFTS = {"follow_up_provider": FOLLOW_UP_ENV, "call_prep_provider": CALL_PREP_ENV}
ATTACHMENT_ENV = "PIPELINE_OUTREACH_ATTACHMENT"
RESEARCH_AGENTS = ("claude-code", "codex-cli")
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
        catalog = provider_catalog()
        drafts = [
            {"id": item["id"], "label": item["display_name"], "available": item["configured"], "hint": item["setup_hint"]}
            for item in catalog
        ] + [LEGACY_OPTION]
        research = [option for option in drafts if option["id"] in RESEARCH_AGENTS]
        attached = attachment_path()
        try:
            from .outreach_review import review_choice

            chosen, note = review_choice()
            automatic_review = {"id": chosen, "note": note, "problem": ""}
        except ValueError as exc:
            automatic_review = {"id": "", "note": "", "problem": str(exc)}
        following = {
            key: {"value": os.environ.get(env, "").strip(), "options": drafts}
            for key, env in FOLLOWING_DRAFTS.items()
        }
        return {
            **following,
            "review_provider": {
                "value": os.environ.get(REVIEW_ENV, "").strip(),
                # On Automatic: which model reviews now, and why.
                "automatic": automatic_review,
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
        from .setup import set_env_values

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
