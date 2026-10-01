"""Request models for resumes."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ResumeConfirmRequest(BaseModel):
    confirmed_data: dict[str, Any] = Field(default_factory=dict)
    profile_updates: dict[str, Any] = Field(default_factory=dict)
    confirmed_profile_fields: list[str] = Field(default_factory=list, max_length=100)


class ResumeVariantRequest(BaseModel):
    # The student's own name for this résumé's kind of role; empty stops it being a variant.
    variant_label: str = Field(default="", max_length=60)
