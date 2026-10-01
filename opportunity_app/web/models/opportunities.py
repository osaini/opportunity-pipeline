"""Request and response models for opportunities, intent, deadlines, company tags and resume picks."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class OpportunityListResponse(BaseModel):
    items: list[dict[str, Any]]
    total: int
    limit: int
    offset: int
    sort: str
    # The per-employer cap applied (0: none). Items carry `company_total` when set.
    per_company: int = 0
    # False while the caller's profile has no scoring inputs, so every score is
    # the base score and no match reasons exist yet.
    personalized: bool = True


class IntentRequest(BaseModel):
    action: Literal["seen", "saved", "passed", "apply_opened", "undo"]


class InboxSuggestionsRequest(BaseModel):
    enabled: bool


class DeadlineRequest(BaseModel):
    deadline_on: str = Field(min_length=10, max_length=10)
    note: str = Field(default="", max_length=200)


class CompanyTagRequest(BaseModel):
    # The company as displayed on the opportunity; matched on the stored fold.
    company: str = Field(min_length=1, max_length=200)
    tag: str = Field(min_length=1, max_length=25)
    # False removes the tag (an automatic one stays removed after a refresh).
    present: bool


class EarlyProgramStatusRequest(BaseModel):
    status: Literal["todo", "applied", "skipped"]


class ResumePickRequest(BaseModel):
    resume_file_id: str = Field(min_length=1, max_length=200)
