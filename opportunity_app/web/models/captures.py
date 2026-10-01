"""Request models for opportunity captures."""

from __future__ import annotations

from pydantic import BaseModel, Field


class CaptureUrlRequest(BaseModel):
    url: str = Field(min_length=8, max_length=2_000)


class CaptureConfirmRequest(BaseModel):
    company: str = Field(min_length=1, max_length=300)
    title: str = Field(min_length=1, max_length=500)
    url: str = Field(min_length=8, max_length=2_000)
    location: str = Field(default="", max_length=500)
    role_type: str = Field(default="other", max_length=80)
    description: str = Field(default="", max_length=200_000)
