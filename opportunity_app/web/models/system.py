"""Request and response models for the manual refresh and job-board sources."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class RefreshStepResponse(BaseModel):
    key: str
    label: str
    state: Literal["pending", "running", "done", "failed", "skipped"]
    done: int
    total: int
    detail: str


class RefreshStatusResponse(BaseModel):
    available: bool
    state: Literal["idle", "running", "succeeded", "failed"]
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None
    steps: list[RefreshStepResponse]


class BoardLookupRequest(BaseModel):
    company: str = Field(min_length=1, max_length=120)


class BoardAddRequest(BaseModel):
    lookup_id: str = Field(min_length=1, max_length=64)
    student_confirmed: bool = False
