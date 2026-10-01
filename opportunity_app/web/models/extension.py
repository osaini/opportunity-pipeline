"""Request models for the Chrome extension and apply sessions."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class ApplySessionRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=200)
    application_id: str | None = Field(default=None, max_length=500)
    page_url: str = Field(min_length=8, max_length=2_000)
    ats_type: str = Field(default="generic", max_length=100)
    fields: list[dict[str, Any]] = Field(default_factory=list, max_length=500)
    status: Literal["draft", "reviewed", "completed"] = "draft"


class ExtensionPairingRedeemRequest(BaseModel):
    code: str = Field(min_length=20, max_length=200)
    device_name: str = Field(default="Chrome Apply Mode", max_length=120)


class ExtensionSessionRequest(BaseModel):
    application_id: str | None = Field(default=None, max_length=500)
    page_url: str = Field(min_length=8, max_length=2_000)
    ats_type: str = Field(default="generic", max_length=100)
    fields: list[dict[str, Any]] = Field(default_factory=list, max_length=500)
    status: Literal["draft", "reviewed", "completed"] = "draft"


class ExtensionStepRequest(BaseModel):
    page_url: str = Field(min_length=8, max_length=2_000)
    ats_type: str = Field(default="generic", max_length=100)
    fields: list[dict[str, Any]] = Field(default_factory=list, max_length=500)
    summary: dict[str, int] = Field(default_factory=dict)
    status: Literal["scanned", "reviewed", "filled", "manual", "completed"] = "scanned"


class ExtensionAnswerRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2_000)
    answer: str = Field(min_length=1, max_length=50_000)
    company: str = Field(default="", max_length=300)
    tags: list[str] = Field(default_factory=list, max_length=100)
