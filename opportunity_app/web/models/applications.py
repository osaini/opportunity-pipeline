"""Request models for the application tracker."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ApplicationUpdateRequest(BaseModel):
    stage: Literal["applying", "applied", "interview", "offer", "rejected", "withdrawn", "archived"] | None = None
    notes: str | None = Field(default=None, max_length=10_000)
    follow_up_at: str | None = Field(default=None, max_length=80)
    timezone: str = Field(default="UTC", min_length=1, max_length=100)


class ContactCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    role: str = Field(default="", max_length=200)
    email: str = Field(default="", max_length=320)
    phone: str = Field(default="", max_length=80)


class TaskCreateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    due_at: str | None = Field(default=None, max_length=80)
    # The browser's IANA zone, so a datetime-local value is stored as an instant.
    timezone: str | None = Field(default=None, min_length=1, max_length=100)


class TaskUpdateRequest(BaseModel):
    status: Literal["open", "done"]
