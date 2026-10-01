"""Request models for the profile."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ProfileUpdateRequest(BaseModel):
    updates: dict[str, Any]
    confirmed_fields: list[str] = Field(default_factory=list, max_length=100)
